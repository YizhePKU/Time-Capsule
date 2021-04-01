import logging
from concurrent.futures import ThreadPoolExecutor
import grpc

from service.scheduler_pb2_grpc import SchedulerServicer, add_SchedulerServicer_to_server
from service import scheduler_pb2

class MyScheduler(SchedulerServicer):
    def SaveUrl(self, request, context):
        return scheduler_pb2.SaveUrlResponse(status_code=200)

def serve():
    server = grpc.server(ThreadPoolExecutor(max_workers=10))
    add_SchedulerServicer_to_server(MyScheduler(), server)
    server.add_insecure_port('0.0.0.0:50051')
    server.start()
    server.wait_for_termination()

if __name__ == '__main__':
    logging.basicConfig()
    serve()
