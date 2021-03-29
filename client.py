import grpc

from rpc.scheduler_pb2_grpc import SchedulerStub
from rpc import scheduler_pb2

def run():
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = SchedulerStub(channel)
        response = stub.SaveUrl(scheduler_pb2.Url(url='www.bing.com'))
    print(f"Client received: {response.status_code}")

if __name__ == '__main__':
    run()
