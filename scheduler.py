import os
import time
import threading
import sqlite3
import uuid
import hashlib
import logging
import time
from pathlib import Path
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
schema_file = 'db/schema'

def unix_time():
    return int(time.time())

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

def initialize_db():
    path = Path(db_file)
    if not path.exists():
        path.touch()
        conn = sqlite3.connect(db_file)
        with open(schema_file) as file:
            schema = file.read()
            conn.executescript(schema)

class MyScheduler(SchedulerServicer):
    def __init__(self):
        initialize_db()

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

    def select_worker(self):
        assert len(self.worker_pool) > 0
        chan = grpc.insecure_channel(self.worker_pool[0])
        return WorkerStub(chan)

    def select_storage(self):
        assert len(self.storage_pool) > 0
        chan = grpc.insecure_channel(self.storage_pool[0])
        return StorageStub(chan)
    
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
        username = request.name

        logging.info(f'ConfirmLogin: token={token}, openid={openid}')
        with self.lock:
            if token not in self.try_logins:
                context.abort(StatusCode.INVALID_ARGUMENT, "Non-existing token")
            self.try_logins[token].set()
            self.sessions[token] = openid

        with self.db_connect() as db:
            cnt = db.execute('SELECT COUNT(*) FROM users WHERE users.openid = ?', (openid,)).fetchone()[0]
            if cnt == 0:
                db.execute('INSERT INTO users VALUES (?,?)', (openid, username))
        return Empty()

    @requires_token
    def GetUserData(self, request, context):
        openid = context.openid
        logging.info(f'GetUserData: openid={openid}')

        with self.db_connect() as db:
            username = db.execute('SELECT name FROM users WHERE openid = ?', (openid,)).fetchone()['name']

            articles = []
            for row in db.execute('SELECT id, title, created_at FROM articles WHERE articles.user = ?', (openid,)):
                cnt = db.execute('SELECT COUNT(*) FROM snapshots WHERE snapshots.article = ?', (row['id'],)).fetchone()[0]
                articles.append(co.Article(id=row['id'], title=row['title'], created_at=row['created_at'], snapshot_count=cnt))

            notifications = []
            for r in db.execute('SELECT id, created_at, has_read, content, type FROM notifications WHERE user = ?', (openid,)):
                notifications.append(co.Notification(id=r['id'], created_at=r['created_at'], has_read=r['has_read'], content=r['content'], type=r['type']))

        return co.UserData(username=username, articles=articles, notifications=notifications)

    @requires_token
    def CreateArticle(self, request, context):
        openid = context.openid
        title = request.title
        logging.info(f'CreateArticle: title={title}')

        _id = make_uuid()
        timestamp = unix_time()
        with self.db_connect() as db:
            db.execute('INSERT INTO articles VALUES (?,?,?,?)', (_id, openid, timestamp, title))
        return co.Article(id=_id, title=title, created_at=timestamp)

    @requires_token
    def DeleteArticle(self, request, context):
        openid = context.openid
        article_id = request.article_id
        logging.info(f'DeleteArticle: id={article_id}')

        with self.db_connect() as db:
            db.execute('DELETE FROM articles WHERE articles.id = ? AND articles.user = ?', (article_id, openid))
        return Empty()

    @requires_token
    def ChangeArticleTitle(self, request, context):
        openid = context.openid
        article_id = request.article_id
        title = request.title
        logging.info(f'ChangeArticleTitle: id={article_id}, new title={title}')

        with self.db_connect() as db:
            db.execute('UPDATE articles SET title = ? WHERE articles.id = ? AND articles.user = ?', (article_id, openid))
        return Empty()

    @requires_token
    def RemoveSnapshotFromArticle(self, request, context):
        openid = context.openid
        article_id = request.article_id
        snapshot_id = request.snapshot_id
        logging.info(f'RemoveSnapshotFromArticle: article_id={article_id}, snapshot_id={snapshot_id}')

        with self.db_connect() as db:
            db.execute('DELETE FROM snapshots WHERE snapshots.uuid = ? AND snapshots.article = ?', (snapshot_id, article_id))
        return Empty()

    @requires_token
    def GetArticleSnapshots(self, request, context):
        openid = context.openid
        article_id = request.article_id
        logging.info(f'GetArticleSnapshots: article_id={article_id}')

        snapshots = []
        with self.db_connect() as db:
            for r in db.execute('SELECT uuid, hash, url, timestamp FROM snapshots WHERE snapshots.article = ?1 AND articles.id = ?1 AND articles.user = ?2', (article_id, openid)):
                snapshots.append(co.Snapshot(id=r['uuid'], hash=r['hash'], url=r['url'], timestamp=r['timestamp'], status=co.Snapshot.Status.ok))
        return sc.GetArticleSnapshotsResponse(snapshots=snapshots)

    @requires_token
    def Capture(self, request, context):
        openid = context.openid
        urls = request.urls
        article_id = request.article_id
        logging.info(f'Capture: {urls}')

        worker = self.select_worker()
        # We need to do this async
        for res in worker.Crawl(wo.CrawlRequest(urls=urls)):
            content = res.content

            storage = self.select_storage()
            storage_key = make_uuid()
            storage.StoreContent(st.StoreRequest(key=storage_key, data=content.data))

            timestamp = unix_time()
            _id = make_uuid()
            _hash = sha256(content.data)
            ledger_key = ledger.add(_hash)

            with self.db_connect() as db:
                db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?)', (_id, article_id, url, _hash, timestamp, ledger_key))
                db.execute('INSERT INTO data VALUES (?,?,?)', (_id, content.type, storage_key))
        return Empty()

    @requires_token
    def GetActiveTasks(self, request, context):
        logging.info(f'GetActiveTasks')
        task = co.Task(id=b'afwfh32ofho2ho', url='bing.com', status=co.Task.Status.working, article_id=b'afwfh32ofho2ho2')
        tasks = [task]
        while True:
            yield sc.CurrentTasks(tasks=tasks)
            tasks.append(task)
            time.sleep(2)

    @requires_token
    def MarkAllAsRead(self, request, context):
        logging.info(f'MarkAllAsRead')
        return Empty()

    def GetSnapshot(self, request, context):
        logging.info(f'GetSnapshot')
        return co.Content(
            type=co.Content.Type.html,
            data=b'<body>Hello world</body>',
        )

    def ListSnapshots(self, request, context):
        url = request.url
        logging.info(f'ListSnapshots: {url}')

        snapshots = []
        with self.db_connect() as db:
            for r in db.execute('SELECT uuid, hash, url, timestamp FROM snapshots WHERE snapshots.url = ?', (url,)):
                snapshots.append(co.Snapshot(id=r['uuid'], hash=r['hash'], url=r['url'], timestamp=r['timestamp'], status=co.Snapshot.Status.ok))
        return sc.Snapshots(snapshots=snapshots)

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
