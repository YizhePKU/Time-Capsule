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
from queue import Queue
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

def make_timestamp():
    return int(time.time())


def make_uuid():
    return uuid.uuid4().bytes


def sha256(data: bytes):
    return hashlib.sha256(data).digest()


def db_connect():
    db_path = Path('db/test.db')
    schema_path = Path('db/schema')

    # Initialize database if necessary
    if not db_path.exists():
        db_path.touch()
        conn = sqlite3.connect(db_path)
        with open(schema_path) as file:
            schema = file.read()
            conn.executescript(schema)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def requires_token(f):
    '''Decorator that checks token in request metadata and puts an openid in context.'''
    def _get_metadata(context):
        return dict(context.invocation_metadata())

    def inner(self, request, context):
        metadata = _get_metadata(context)
        if 'token' not in metadata:
            context.abort(StatusCode.UNAUTHORIZED, "Auth token needed")
        token = metadata['token']
        try:
            context.openid = self.auth.get_openid(token)
        except:
            context.abort(StatusCode.UNAUTHORIZED, "Bad token")
        return f(self, request, context)

    return inner


class Auth:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending_tokens = {} # token -> event
        self.confirmed_tokens = {} # token -> openid

    def new_login_token(self, token):
        '''Add a new token awaiting confirmation.
           Returns whether confirmation is successful.
           Raise if the token is invalid.
        '''
        with self.lock:
            if token in self.pending_tokens or token in self.confirmed_tokens:
                raise Exception()
            event = threading.Event()
            self.pending_tokens[token] = event

        result = event.wait(timeout=30)
        with self.lock:
            del self.pending_tokens[token]
        return result

    def confirm_login_token(self, token, openid):
        '''Confirm a token and associate it with an openid.
           Raise if the token does not exist.
        '''
        with self.lock:
            if token not in self.pending_tokens:
                raise Exception()
            self.pending_tokens[token].set()
            self.confirmed_tokens[token] = openid

    def get_openid(self, token):
        '''Returns openid corresponding to a confirmed token.
           Raise if the token does not exist.
        '''
        if token not in self.confirmed_tokens:
            raise Exception()
        return self.confirmed_tokens[token]


class MyScheduler(SchedulerServicer):
    def __init__(self):
        self.auth = Auth()
        self.task_monitor = TaskMonitor()

        self.worker_pool = []
        self.storage_pool = []

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

        try:
            if self.auth.new_login_token(token):
                return Empty()
            else:
                context.abort(StatusCode.DEADLINE_EXCEEDED, "Confirmation timeout")
        except:
            context.abort(StatusCode.INVALID_ARGUMENT, "Token is in use")

    def ConfirmLogin(self, request, context):
        token = request.token
        openid = request.openid
        username = request.name
        logging.info(f'ConfirmLogin: token={token}, openid={openid}')

        try:
            self.auth.confirm_login_token(token, openid)
        except:
            context.abort(StatusCode.INVALID_ARGUMENT, "Non-existing token")

        with db_connect() as db:
            cnt = db.execute('SELECT COUNT(*) FROM users WHERE users.openid = ?', (openid,)).fetchone()[0]
            if cnt == 0:
                db.execute('INSERT INTO users VALUES (?,?)', (openid, username))
        return Empty()

    @requires_token
    def GetUserData(self, request, context):
        openid = context.openid
        logging.info(f'GetUserData: openid={openid}')

        with db_connect() as db:
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
        timestamp = make_timestamp()
        with db_connect() as db:
            db.execute('INSERT INTO articles VALUES (?,?,?,?)', (_id, openid, timestamp, title))
        return co.Article(id=_id, title=title, created_at=timestamp)

    @requires_token
    def DeleteArticle(self, request, context):
        openid = context.openid
        article_id = request.article_id
        logging.info(f'DeleteArticle: id={article_id}')

        with db_connect() as db:
            db.execute('DELETE FROM articles WHERE articles.id = ? AND articles.user = ?', (article_id, openid))
        return Empty()

    @requires_token
    def ChangeArticleTitle(self, request, context):
        openid = context.openid
        article_id = request.article_id
        title = request.title
        logging.info(f'ChangeArticleTitle: id={article_id}, new title={title}')

        with db_connect() as db:
            db.execute('UPDATE articles SET title = ? WHERE articles.id = ? AND articles.user = ?', (article_id, openid))
        return Empty()

    @requires_token
    def RemoveSnapshotFromArticle(self, request, context):
        openid = context.openid
        article_id = request.article_id
        snapshot_id = request.snapshot_id
        logging.info(f'RemoveSnapshotFromArticle: article_id={article_id}, snapshot_id={snapshot_id}')

        with db_connect() as db:
            db.execute('DELETE FROM snapshots WHERE snapshots.uuid = ? AND snapshots.article = ?', (snapshot_id, article_id))
        return Empty()

    @requires_token
    def GetArticleSnapshots(self, request, context):
        openid = context.openid
        article_id = request.article_id
        logging.info(f'GetArticleSnapshots: article_id={article_id}')

        snapshots = []
        with db_connect() as db:
            for r in db.execute('SELECT uuid, hash, url, timestamp FROM snapshots WHERE snapshots.article = ?1 AND articles.id = ?1 AND articles.user = ?2', (article_id, openid)):
                snapshots.append(co.Snapshot(id=r['uuid'], hash=r['hash'], url=r['url'], timestamp=r['timestamp'], status=co.Snapshot.Status.ok))
        return sc.GetArticleSnapshotsResponse(snapshots=snapshots)

    def _async_handle_responses(self, responses, storage, tasks):
        for res in responses:
            url = res.url
            task_id = tasks[url]
            content = res.content
            storage_key = make_uuid()
            storage.StoreContent(st.StoreRequest(key=storage_key, data=content.data))

            timestamp = make_timestamp()
            _id = make_uuid()
            _hash = sha256(content.data)
            ledger_key = ledger.add(_hash)
            with db_connect() as db:
                db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?)', (_id, article_id, url, _hash, timestamp, ledger_key))
                db.execute('INSERT INTO data VALUES (?,?,?)', (_id, content.type, storage_key))
                db.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            self.task_monitor.notify()

    @requires_token
    def Capture(self, request, context):
        openid = context.openid
        urls = request.urls
        article_id = request.article_id
        logging.info(f'Capture: {urls}')

        worker = self.select_worker()
        storage = self.select_storage()
        tasks = {} # url -> task_id
        with db_connect() as db:
            for url in urls:
                task_id = make_uuid()
                tasks[url] = task_id
                status = 1 # Always in working status
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', (task_id, openid, url, status, article_id))
        responses = worker.Crawl(wo.CrawlRequest(urls=urls))
        threading.Thread(target=lambda: self._async_handle_responses(responses, storage, tasks)).run()
        self.task_monitor.notify()
        return Empty()

    def _get_current_tasks(self, openid):
        tasks = []
        with db_connect() as db:
            r = db.execute('SELECT id, url, status, article_id FROM tasks WHERE user = ?', (openid,))
            task = co.Task(id=r['id'], url=r['url'], status=r['status'], article_id=r['article_id'])
            tasks.append(task)
        return sc.CurrentTasks(tasks=tasks)

    @requires_token
    def GetActiveTasks(self, request, context):
        openid = context.openid
        logging.info(f'GetActiveTasks')
        try:
            queue = self.task_monitor.register()
            while queue.get(timeout=300):
                yield self._get_current_tasks(openid)
        finally:
            self.task_monitor.unregister(queue)

    @requires_token
    def MarkAllAsRead(self, request, context):
        logging.info(f'MarkAllAsRead')
        return Empty()

    def GetSnapshot(self, request, context):
        snapshot_id = request.id
        url = request.url
        logging.info(f'GetSnapshot: id={snapshot_id}, url={url}')

        with db_connect() as db:
            r = db.execute('SELECT type, storage_key FROM data WHERE snapshot = ?', (snapshot_id,)).fetchone()
        storage = self.select_storage()
        data = storage.GetContent(st.StorageKey(key=r['storage_key'])).data
        content = Co.Content(type=r['type'], data=data)
        return content

    def ListSnapshots(self, request, context):
        url = request.url
        logging.info(f'ListSnapshots: {url}')

        snapshots = []
        with db_connect() as db:
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


class TaskMonitor:
    def __init__(self):
        # id -> queue
        self.listeners = {}
        self.lock = threading.Lock()

    def register(self):
        '''Register as a listener of task update events. Returns a Queue for incoming events.'''
        queue = Queue()
        queue.id = make_uuid()
        with self.lock:
            self.listeners[queue.id] = queue
        return queue

    def unregister(self, queue):
        '''Give the queue back to unregister.'''
        with self.lock:
            del self.listeners[queue.id]

    def notify(self):
        '''Notify all listeners by adding an item to their queue.'''
        with self.lock:
            for queue in self.listeners:
                queue.put(True)


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
