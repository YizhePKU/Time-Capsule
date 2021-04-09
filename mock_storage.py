import logging
from concurrent.futures import ThreadPoolExecutor
import grpc

from capsule.common_pb2 import Empty, Error, Url, Snapshot, SnapshotList, Content, Endpoint
from capsule.storage_pb2_grpc import StorageServicer, add_StorageServicer_to_server
from capsule import storage_pb2
from capsule.scheduler_pb2_grpc import SchedulerStub
from capsule import scheduler_pb2

class MyStorage(StorageServicer):
    def __init__(self):
        self.m = {}

    def StoreContent(self, request, context):
        uuid = request.meta.uuid
        data = request.data
        self.m[uuid] = data
        return Empty()

    def GetContent(self, request, context):
        uuid = request.uuid
        return Content(data=self.m[uuid])


def serve():
    server = grpc.server(ThreadPoolExecutor(max_workers=10))
    add_StorageServicer_to_server(MyStorage(), server)
    server.add_insecure_port('0.0.0.0:8002')
    server.start()
    server.wait_for_termination()

if __name__ == '__main__':
    logging.basicConfig()
    serve()
