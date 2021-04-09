import grpc

from capsule.common_pb2 import Empty, Error, Url, Snapshot, SnapshotList, Content, Endpoint
from capsule.scheduler_pb2_grpc import SchedulerStub
from capsule import scheduler_pb2

chan = grpc.insecure_channel('localhost:8000')
stub = SchedulerStub(chan)

stub.SaveUrl(Url(url='www.bing.com'))

r = stub.ListSnapshots(Url(url='www.bing.com'))
