import sqlite3
import uuid
import hashlib
import logging
from time import time as unix_time
from concurrent.futures import ThreadPoolExecutor
import grpc
from grpc import StatusCode

import ledger
from capsule.common_pb2 import Empty, Error, Url, Snapshot, SnapshotList, Content, Endpoint
from capsule.scheduler_pb2_grpc import SchedulerServicer, add_SchedulerServicer_to_server
from capsule import scheduler_pb2
from capsule.worker_pb2_grpc import WorkerStub
from capsule import worker_pb2
from capsule.storage_pb2_grpc import StorageStub
from capsule import storage_pb2

db_file = 'db/test.db'

def make_uuid():
    return uuid.uuid4().bytes

def sha256(stuff: bytes):
    m = hashlib.sha256(stuff)
    return m.digest()


class MyScheduler(SchedulerServicer):
    def __init__(self):
        self.worker_pool = ['192.168.43.237:50051']
        self.storage_pool = ['192.168.43.237:50050']

    def db_connect(self):
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def SaveUrl(self, request, context):
        logging.info(f'Save url request: {request.url}')
        if not self.worker_pool:
            context.abort(StatusCode.Unavailable, "No available worker")
        if not self.storage_pool:
            context.abort(StatusCode.Unavailable, "No available storage")

        # Let worker crawl the webpage
        with grpc.insecure_channel(self.worker_pool[0]) as chan:
            worker_stub = WorkerStub(chan)
            data = worker_stub.CrawlUrl(Url(url=request.url)).data

        # Create metadata
        uuid = make_uuid()
        url = request.url
        _hash = sha256(data)
        timestamp = int(unix_time())
        ledger_key = b''
        meta = Snapshot(uuid=uuid, hash=_hash, url=Url(url=url), timestamp=timestamp)

        # Store the webpage and metadata to storage
        with grpc.insecure_channel(self.storage_pool[0]) as chan:
            storage_stub = StorageStub(chan)
            storage_stub.StoreContent(Content(meta=meta, data=data))

        cmd = 'INSERT INTO snapshots (uuid, url, hash, timestamp, ledger_key) VALUES (?, ?, ?, ?, ?)'

        with self.db_connect() as db:
            db.execute(cmd, (uuid, url, _hash, timestamp, ledger_key))
        return Empty()

    def ListSnapshots(self, request, context):
        url = request.url
        cmd = 'SELECT uuid, hash, timestamp FROM snapshots WHERE url = ?'
        snapshots = []
        with self.db_connect() as db:
            for row in db.execute(cmd, (url,)):
                snapshots.append(Snapshot(
                    uuid = row['uuid'],
                    url = Url(url=url),
                    hash = row['hash'],
                    timestamp = int(row['timestamp']),
                ))
        return SnapshotList(snapshots=snapshots)

    def FetchSnapshot(self, request, context):
        if not self.storage_pool:
            context.abort("No available storage")
        with grpc.insecure_channel(self.storage_pool[0]) as chan:
            storage_stub = StorageStub(chan)
            content = storage_stub.GetContent(request)
        return content

    def RegisterWorker(self, request, context):
        addr = request.addr
        port = request.port
        logging.info(f'Worker registered: {addr}:{port}')
        self.worker_pool.append(f'{addr}:{port}')
        return Empty()

    def RegisterStorage(self, request, context):
        addr = request.addr
        port = request.port
        logging.info(f'Storage registered: {addr}:{port}')
        self.storage_pool.append(f'{addr}:{port}')
        return Empty()


def serve(port=8000):
    server = grpc.server(ThreadPoolExecutor(max_workers=10))
    add_SchedulerServicer_to_server(MyScheduler(), server)
    server.add_insecure_port(f'127.0.0.1:{port}')
    server.start()
    logging.info(f'Server started at port {port}')
    server.wait_for_termination()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    serve()
