import os
import time
import threading
import sqlite3
import uuid
import hashlib
import logging
from time import time as unix_time
from concurrent.futures import ThreadPoolExecutor
import grpc
from grpc import StatusCode

import ledger

from capsule import common_pb2 as co
from capsule.common_pb2 import Empty

from capsule import scheduler_pb2 as sc
from capsule.scheduler_pb2_grpc import SchedulerServicer, add_SchedulerServicer_to_server

from capsule import worker_pb2 as wo
from capsule.worker_pb2_grpc import WorkerStub

from capsule import storage_pb2 as st
from capsule.storage_pb2_grpc import StorageStub

db_file = 'db/test.db'

def make_uuid():
    return uuid.uuid4().bytes

def sha256(stuff: bytes):
    m = hashlib.sha256(stuff)
    return m.digest()

def get_metadata(context):
    return dict(context.invocation_metadata())

def requires_token(f):
    def inner(self, request, context):
        metadata = get_metadata(context)
        if 'token' not in metadata:
            context.abort(StatusCode.UNAUTHORIZED, "Auth token needed")
        token = metadata['token']
        with self.lock:
            if token not in self.sessions:
                context.abort(StatusCode.UNAUTHORIZED, "Bad token")
            else:
                context.openid = self.sessions[token]
        return f(self, request, context)
    return inner

class MyScheduler(SchedulerServicer):
    def __init__(self):
        # Protects try_logins and sessions
        self.lock = threading.Lock()
        # token -> event
        self.try_logins = {}
        # token -> openid
        self.sessions = {}

        self.worker_pool = []
        self.storage_pool = []

    def db_connect(self):
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        return conn
    
    def TryLogin(self, request, context):
        token = request.token
        logging.info(f'TryLogin: token={token}')
        with self.lock:
            if token in self.sessions:
                context.abort(StatusCode.INVALID_ARGUMENT, "Token is in use")
            if token in self.try_logins:
                context.abort(StatusCode.INVALID_ARGUMENT, "Duplicate login request")
            event = threading.Event()
            self.try_logins[token] = event
        confirmed = event.wait(timeout=30)
        with self.lock:
            del self.try_logins[token]
        if confirmed:
            return Empty()
        else:
            context.abort(StatusCode.DEADLINE_EXCEEDED, "Confirmation timeout")

    def ConfirmLogin(self, request, context):
        token = request.token
        openid = request.openid
        logging.info(f'ConfirmLogin: token={token}, openid={openid}')
        with self.lock:
            if token not in self.try_logins:
                context.abort(StatusCode.INVALID_ARGUMENT, "Non-existing token")
            self.try_logins[token].set()
            self.sessions[token] = openid
        return Empty()

    @requires_token
    def GetUserData(self, request, context):
        logging.info(f'GetUserData: openid={context.openid}')
        username = 'YizhePKU'
        articles = [
            co.Article(id=b'afwfh32ofho2ho2', title='Article 1', created_at= 123123, snapshot_count=2),
            co.Article(id=b'afjekljbkjglkjk', title='Article 2', created_at= 123123, snapshot_count=5),
        ]
        notifications = [
            co.Notification(id=b'fdfheohfshe', created_at=123123, has_read=True, content="Message 1", type=co.Notification.Type.info),
            co.Notification(id=b'dfjkefovnwon', created_at=123123, has_read=False, content="Message 2", type=co.Notification.Type.error),
        ]
        return co.UserData(username=username, articles=articles, notifications=notifications)

    @requires_token
    def CreateArticle(self, request, context):
        logging.info(f'CreateArticle')
        return co.Article(id=b'afwfh32ofho2ho2', title='Article 1', created_at= 123123, snapshots=[]),

    @requires_token
    def DeleteArticle(self, request, context):
        logging.info(f'DeleteArticle')
        return Empty()

    @requires_token
    def ChangeArticleTitle(self, request, context):
        logging.info(f'ChangeArticleTitle')
        return Empty()

    @requires_token
    def RemoveSnapshotFromArticle(self, request, context):
        logging.info(f'RemoveSnapshotFromArticle')
        return Empty()

    @requires_token
    def GetArticleSnapshots(self, request, context):
        logging.info(f'GetArticleSnapshots')
        return sc.GetArticleSnapshotsResponse([
            co.Snapshot(id=b'fekvlehflw', hash=b'flklkj3lkl', url='bing.com', timestamp=234234, status=co.Snapshot.Status.ok),
            co.Snapshot(id=b'jlevlkwjl', hash=b'dfkwjvwll', url='github.com', timestamp=234234, status=co.Snapshot.Status.dead),
        ])

    @requires_token
    def Capture(self, request, context):
        logging.info(f'Capture')
        return Empty()

    @requires_token
    def GetActiveTasks(self, request, context):
        logging.info(f'GetActiveTasks')
        task = co.Task(id=b'afwfh32ofho2ho', url='bing.com', status=co.Task.Status.working, article_id=b'asfhejwef')
        tasks = [task]
        while True:
            yield
            tasks.append(task)
            time.sleep(2)

    @requires_token
    def MarkAllAsRead(self, request, context):
        logging.info(f'MarkAllAsRead')
        return Empty()

    def FetchSnapshot(self, request, context):
        logging.info(f'FetchSnapshot')
        return co.Content(
            id=b'sdfewjfewfsdf',
            html="<body>Hello world</body>",
            header="",
        )

    def ListSnapshots(self, request, context):
        url = request.url
        logging.info(f'ListSnapshots: {url}')
        snapshot = co.Snapshot(
            sid = b'asdfhelhlsfje',
            hash = b'sdfehwlldfj',
            url = url,
            timestamp = 42,
            status = co.Snapshot.Status.ok,
        )
        return co.Snapshots([snapshot, snapshot])

    # def SaveUrl(self, request, context):
    #     logging.info(f'Save url request: {request.url}')
    #     if not self.worker_pool:
    #         context.abort(StatusCode.Unavailable, "No available worker")
    #     if not self.storage_pool:
    #         context.abort(StatusCode.Unavailable, "No available storage")

    #     # Let worker crawl the webpage
    #     with grpc.insecure_channel(self.worker_pool[0]) as chan:
    #         worker_stub = WorkerStub(chan)
    #         data = worker_stub.CrawlUrl(Url(url=request.url)).data

    #     # Create metadata
    #     uuid = make_uuid()
    #     url = request.url
    #     _hash = sha256(data)
    #     timestamp = int(unix_time())
    #     ledger_key = b''
    #     meta = Snapshot(uuid=uuid, hash=_hash, url=Url(url=url), timestamp=timestamp)

    #     # Store the webpage and metadata to storage
    #     with grpc.insecure_channel(self.storage_pool[0]) as chan:
    #         storage_stub = StorageStub(chan)
    #         storage_stub.StoreContent(Content(meta=meta, data=data))

    #     cmd = 'INSERT INTO snapshots (uuid, url, hash, timestamp, ledger_key) VALUES (?, ?, ?, ?, ?)'

    #     with self.db_connect() as db:
    #         db.execute(cmd, (uuid, url, _hash, timestamp, ledger_key))
    #     return Empty()

    # def ListSnapshots(self, request, context):
    #     url = request.url
    #     cmd = 'SELECT uuid, hash, timestamp FROM snapshots WHERE url = ?'
    #     snapshots = []
    #     with self.db_connect() as db:
    #         for row in db.execute(cmd, (url,)):
    #             snapshots.append(Snapshot(
    #                 uuid = row['uuid'],
    #                 url = Url(url=url),
    #                 hash = row['hash'],
    #                 timestamp = int(row['timestamp']),
    #             ))
    #     return SnapshotList(snapshots=snapshots)

    # def FetchSnapshot(self, request, context):
    #     if not self.storage_pool:
    #         context.abort("No available storage")
    #     with grpc.insecure_channel(self.storage_pool[0]) as chan:
    #         storage_stub = StorageStub(chan)
    #         content = storage_stub.GetContent(request)
    #     return content

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
    server.add_insecure_port(f'0.0.0.0:{port}')
    server.start()
    logging.info(f'Scheduler listening at 0.0.0.0:{port}')
    server.wait_for_termination()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    port = os.getenv('SCHEDULER_PORT', 8848)
    serve(port)
