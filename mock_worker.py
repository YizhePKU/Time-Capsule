import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor
import grpc

from capsule.common_pb2 import Empty, Error, Url, Snapshot, SnapshotList, Content, Endpoint
from capsule.worker_pb2_grpc import WorkerServicer, add_WorkerServicer_to_server
from capsule import worker_pb2
from capsule.scheduler_pb2_grpc import SchedulerStub
from capsule import scheduler_pb2

class MyWorker(WorkerServicer):
    def CrawlUrl(self, request, context):
        url = request.url
        return Content(data=b'Data created by worker')


def serve():
    server = grpc.server(ThreadPoolExecutor(max_workers=10))
    add_WorkerServicer_to_server(MyWorker(), server)
    server.add_insecure_port('0.0.0.0:8001')
    server.start()
    server.wait_for_termination()

if __name__ == '__main__':
    logging.basicConfig()
    serve()
