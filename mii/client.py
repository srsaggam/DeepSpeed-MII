# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
import asyncio
import grpc
import requests
import mii
from mii.utils import get_task
from mii.grpc_related.proto import modelresponse_pb2, modelresponse_pb2_grpc
from mii.constants import GRPC_MAX_MSG_SIZE, Tasks
from mii.method_table import GRPC_METHOD_TABLE


def _get_deployment_info(deployment_name):
    configs = mii.utils.import_score_file(deployment_name).configs
    task = configs[mii.constants.TASK_NAME_KEY]
    mii_configs_dict = configs[mii.constants.MII_CONFIGS_KEY]
    mii_configs = mii.config.MIIConfig(**mii_configs_dict)

    assert task is not None, "The task name should be set before calling init"
    return task, mii_configs


def mii_query_handle(deployment_name):
    """Get a query handle for a local deployment:

        mii/examples/local/gpt2-query-example.py
        mii/examples/local/roberta-qa-query-example.py

    Arguments:
        deployment_name: Name of the deployment. Used as an identifier for posting queries for ``LOCAL`` deployment.

    Returns:
        query_handle: A query handle with a single method `.query(request_dictionary)` using which queries can be sent to the model.
    """

    if deployment_name in mii.non_persistent_models:
        inference_pipeline, task = mii.non_persistent_models[deployment_name]
        return MIINonPersistentClient(task, deployment_name)

    task_name, mii_configs = _get_deployment_info(deployment_name)
    return MIIClient(task_name, "localhost", mii_configs.port_number)


def create_channel(host, port):
    return grpc.aio.insecure_channel(f'{host}:{port}',
                                     options=[('grpc.max_send_message_length',
                                               GRPC_MAX_MSG_SIZE),
                                              ('grpc.max_receive_message_length',
                                               GRPC_MAX_MSG_SIZE)])


class MIIClient():
    """
    Client to send queries to a single endpoint.
    """
    def __init__(self, task_name, host, port):
        self.asyncio_loop = asyncio.get_event_loop()
        channel = create_channel(host, port)
        self.stub = modelresponse_pb2_grpc.ModelResponseStub(channel)
        self.task = get_task(task_name)

    async def _request_async_response(self, request_dict, **query_kwargs):
        if self.task not in GRPC_METHOD_TABLE:
            raise ValueError(f"unknown task: {self.task}")

        task_methods = GRPC_METHOD_TABLE[self.task]
        proto_request = task_methods.pack_request_to_proto(request_dict, **query_kwargs)
        proto_response = await getattr(self.stub, task_methods.method)(proto_request)
        return task_methods.unpack_response_from_proto(proto_response)

    def query(self, request_dict, **query_kwargs):
        return self.asyncio_loop.run_until_complete(
            self._request_async_response(request_dict,
                                         **query_kwargs))

    async def terminate_async(self):
        await self.stub.Terminate(
            modelresponse_pb2.google_dot_protobuf_dot_empty__pb2.Empty())

    def terminate(self):
        self.asyncio_loop.run_until_complete(self.terminate_async())

    async def create_session_async(self, session_id):
        return await self.stub.CreateSession(
            modelresponse_pb2.SessionID(session_id=session_id))

    def create_session(self, session_id):
        assert self.task == Tasks.TEXT_GENERATION, f"Session creation only available for task '{Tasks.TEXT_GENERATION}'."
        return self.asyncio_loop.run_until_complete(
            self.create_session_async(session_id))

    async def destroy_session_async(self, session_id):
        await self.stub.DestroySession(modelresponse_pb2.SessionID(session_id=session_id)
                                       )

    def destroy_session(self, session_id):
        assert self.task == Tasks.TEXT_GENERATION, f"Session deletion only available for task '{Tasks.TEXT_GENERATION}'."
        self.asyncio_loop.run_until_complete(self.destroy_session_async(session_id))


class MIITensorParallelClient():
    """
    Client to send queries to multiple endpoints in parallel.
    This is used to call multiple servers deployed for tensor parallelism.
    """
    def __init__(self, task_name, host, ports):
        self.task = get_task(task_name)
        self.clients = [MIIClient(task_name, host, port) for port in ports]
        self.asyncio_loop = asyncio.get_event_loop()

    # runs task in parallel and return the result from the first task
    async def _query_in_tensor_parallel(self, request_string, query_kwargs):
        responses = []
        for client in self.clients:
            responses.append(
                self.asyncio_loop.create_task(
                    client._request_async_response(request_string,
                                                   **query_kwargs)))

        await responses[0]
        return responses[0]

    def query(self, request_dict, **query_kwargs):
        """Query a local deployment:

            mii/examples/local/gpt2-query-example.py
            mii/examples/local/roberta-qa-query-example.py

        Arguments:
            request_dict: A task specific request dictionary consisting of the inputs to the models
            query_kwargs: additional query parameters for the model

        Returns:
            response: Response of the model
        """
        response = self.asyncio_loop.run_until_complete(
            self._query_in_tensor_parallel(request_dict,
                                           query_kwargs))
        ret = response.result()
        return ret

    def terminate(self):
        """Terminates the deployment"""
        for client in self.clients:
            client.terminate()

    def create_session(self, session_id):
        for client in self.clients:
            client.create_session(session_id)

    def destroy_session(self, session_id):
        for client in self.clients:
            client.destroy_session(session_id)


class MIINonPersistentClient():
    def __init__(self, task, deployment_name):
        self.task = task
        self.deployment_name = deployment_name

    def query(self, request_dict, **query_kwargs):
        assert self.deployment_name in mii.non_persistent_models, f"deployment: {self.deployment_name} not found"
        task_methods = GRPC_METHOD_TABLE[self.task]
        inference_pipeline = mii.non_persistent_models[self.deployment_name][0]

        if self.task == Tasks.QUESTION_ANSWERING:
            if 'question' not in request_dict or 'context' not in request_dict:
                raise Exception(
                    "Question Answering Task requires 'question' and 'context' keys")
            args = (request_dict["question"], request_dict["context"])
            kwargs = query_kwargs

        elif self.task == Tasks.CONVERSATIONAL:
            conv = task_methods.create_conversation(request_dict, **query_kwargs)
            args = (conv, )
            kwargs = {}

        else:
            args = (request_dict['query'], )
            kwargs = query_kwargs

        return task_methods.run_inference(inference_pipeline, args, query_kwargs)

    def terminate(self):
        print(f"Terminating {self.deployment_name}...")
        del mii.non_persistent_models[self.deployment_name]


def terminate_restful_gateway(deployment_name):
    _, mii_configs = _get_deployment_info(deployment_name)
    if mii_configs.enable_restful_api:
        requests.get(f"http://localhost:{mii_configs.restful_api_port}/terminate")
