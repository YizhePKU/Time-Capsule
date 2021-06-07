import os
import random
import threading
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import grpc
from grpc import StatusCode

from scheduler import ledger
from scheduler.event import Event
from scheduler.auth import Auth, AuthException
from scheduler.utils import unix_time, uuid, open_db

from capsule import common_pb2 as co

from capsule import scheduler_pb2 as sc
from capsule.scheduler_pb2_grpc import (
    SchedulerServicer,
    add_SchedulerServicer_to_server,
)

from capsule import worker_pb2 as wo
from capsule.worker_pb2_grpc import WorkerStub


def requires_token(f):
    """Decorator that checks token in request metadata and puts an openid in context."""

    def _get_metadata(context):
        return dict(context.invocation_metadata())

    def inner(self, request, context):
        metadata = _get_metadata(context)
        if "token" not in metadata:
            context.abort(StatusCode.UNAUTHORIZED, "Auth token needed")
        token = metadata["token"]
        try:
            context.openid = self.auth.get_openid(token)
        except AuthException:
            context.abort(StatusCode.UNAUTHORIZED, "Bad token")
        return f(self, request, context)

    return inner


def log_request(f):
    def inner(self, request, context):
        logging.info(f'{f.__name__}\n{request}')
        return f(self, request, context)

    return inner


class MyScheduler(SchedulerServicer):
    def __init__(self, auth, task_event, db_fn):
        self.auth = auth
        self.task_event = task_event
        self.db_fn = db_fn
        self.workers = set()

    def worker_stub(self):
        if not self.workers:
            raise Exception('No worker available')
        endpoint = random.choice(list(self.workers))
        return WorkerStub(grpc.insecure_channel(endpoint))

    @log_request
    def TryLogin(self, request, context):
        token = request.token

        try:
            if self.auth.request_login(token):
                return co.Empty()
            else:
                context.abort(StatusCode.DEADLINE_EXCEEDED, "Confirmation timeout")
        except AuthException:
            context.abort(StatusCode.INVALID_ARGUMENT, "Token is in use")

    @log_request
    def ConfirmLogin(self, request, context):
        token = request.token
        openid = request.openid
        username = request.name

        try:
            self.auth.confirm_login(token, openid)
        except AuthException:
            context.abort(StatusCode.INVALID_ARGUMENT, "Non-existing token")

        with self.db_fn() as db:
            cnt = db.execute(
                "SELECT COUNT(*) FROM users WHERE users.openid = ?", (openid,)
            ).fetchone()[0]
            if cnt == 0:
                logging.info(f"Creating new user: username={username}, openid={openid}")
                db.execute("INSERT INTO users VALUES (?,?)", (openid, username))
        return co.Empty()

    @requires_token
    @log_request
    def GetUserData(self, request, context):
        openid = context.openid

        with self.db_fn() as db:
            username = db.execute(
                "SELECT name FROM users WHERE openid = ?", (openid,)
            ).fetchone()["name"]

            articles = []
            for row in db.execute(
                "SELECT id, title, created_at FROM articles WHERE articles.user = ?",
                (openid,),
            ):
                cnt = db.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE snapshots.article = ?",
                    (row["id"],),
                ).fetchone()[0]
                articles.append(
                    co.Article(
                        id=row["id"],
                        title=row["title"],
                        created_at=row["created_at"],
                        snapshot_count=cnt,
                    )
                )

            notifications = []
            for r in db.execute(
                "SELECT id, created_at, has_read, content, type FROM notifications WHERE user = ?",
                (openid,),
            ):
                notifications.append(
                    co.Notification(
                        id=r["id"],
                        created_at=r["created_at"],
                        has_read=r["has_read"],
                        content=r["content"],
                        type=r["type"],
                    )
                )
        r = co.UserData(
            username=username, articles=articles, notifications=notifications
        )
        return r

    @requires_token
    @log_request
    def CreateArticle(self, request, context):
        openid = context.openid
        title = request.title

        _id = uuid()
        timestamp = unix_time()
        with self.db_fn() as db:
            db.execute(
                "INSERT INTO articles VALUES (?,?,?,?)", (_id, openid, timestamp, title)
            )
        return co.Article(id=_id, title=title, created_at=timestamp)

    @requires_token
    @log_request
    def DeleteArticle(self, request, context):
        openid = context.openid
        article_id = request.article_id

        with self.db_fn() as db:
            db.execute(
                "DELETE FROM articles WHERE articles.id = ? AND articles.user = ?",
                (article_id, openid),
            )
        return co.Empty()

    @requires_token
    @log_request
    def ChangeArticleTitle(self, request, context):
        openid = context.openid
        article_id = request.article_id
        title = request.title

        with self.db_fn() as db:
            db.execute(
                "UPDATE articles SET title = ? WHERE articles.id = ? AND articles.user = ?",
                (title, article_id, openid),
            )
        return co.Empty()

    @requires_token
    @log_request
    def RemoveSnapshotFromArticle(self, request, context):
        article_id = request.article_id
        snapshot_id = request.snapshot_id

        with self.db_fn() as db:
            db.execute(
                "DELETE FROM snapshots WHERE snapshots.uuid = ? AND snapshots.article = ?",
                (snapshot_id, article_id),
            )
        return co.Empty()

    @requires_token
    @log_request
    def GetArticleSnapshots(self, request, context):
        openid = context.openid
        article_id = request.article_id

        snapshots = []
        with self.db_fn() as db:
            for r in db.execute(
                "SELECT uuid, url, timestamp FROM snapshots, articles WHERE snapshots.article = ?1 AND articles.id = ?1 AND articles.user = ?2",
                (article_id, openid),
            ):
                snapshots.append(
                    co.Snapshot(
                        id=r["uuid"],
                        url=r["url"],
                        timestamp=r["timestamp"],
                    )
                )
        return sc.GetArticleSnapshotsResponse(snapshots=snapshots)

    def _add_notification(self, openid, msg, is_error):
        with self.db_fn() as db:
            params = {
                'id': uuid(),
                'user': openid,
                'type': 1 if is_error else 0,
                'created_at': unix_time(),
                'has_read': 0,
                'content': msg,
            }
            db.execute('INSERT INTO notifications VALUES ($id, $user, $type, $created_at, $has_read, $content)', params)

    @requires_token
    @log_request
    def Capture(self, request, context):
        openid = context.openid
        urls = list(set(request.urls))
        article_id = request.article_id

        worker = self.worker_stub()
        tasks = {}  # url -> task_id
        with self.db_fn() as db:
            for url in urls:
                task_id = uuid()
                tasks[url] = task_id
                db.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?)",
                    (task_id, openid, url, article_id),
                )

        def _async_action():
            successful_urls = set()
            for res in worker.Crawl(wo.CrawlRequest(urls=urls)):
                url = res.url
                task_id = tasks[url]
                content = res.content
                logging.info(f'Capture succeeded for {url} of type {content.type}')
                timestamp = unix_time()
                _id = uuid()
                ledger_key = ledger.add(content.hash)
                with self.db_fn() as db:
                    # If the snapshot already exists(because this snapshot has multiple pieces of data attached), ignore.
                    db.execute(
                        "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)",
                        (_id, article_id, url, timestamp, False, None),
                    )
                    db.execute(
                        "INSERT INTO data VALUES (?,?,?,?,?)",
                        (_id, content.type, content.data.decode(), content.hash, ledger_key),
                    )
                    db.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
                    successful_urls.add(url)
                self.task_event.notify()
            # Worker hang up
            with self.db_fn() as db:
                for url, task_id in tasks.items():
                    if url not in successful_urls:
                        logging.info(f'Capture failed for {url}')
                        self._add_notification(openid, f"拍摄快照失败：{url}", is_error=True)
            self.task_event.notify()

        threading.Thread(target=_async_action).start()
        self.task_event.notify()
        return co.Empty()

    def _get_current_tasks(self, openid):
        tasks = []
        with self.db_fn() as db:
            for r in db.execute(
                "SELECT id, url, article_id FROM tasks WHERE user = ?",
                (openid,),
            ):
                task = co.Task(
                    id=r["id"],
                    url=r["url"],
                    article_id=r["article_id"],
                )
                tasks.append(task)
        return sc.CurrentTasks(tasks=tasks)

    @requires_token
    @log_request
    def GetActiveTasks(self, request, context):
        openid = context.openid
        yield self._get_current_tasks(openid)
        sem = self.task_event.register()
        # Stream task list continuously, but disconnect if nothing changes for 5 minutes
        while sem.acquire(timeout=300):
            yield self._get_current_tasks(openid)
        self.task_event.unregister(sem)

    @requires_token
    @log_request
    def ClearTasks(self, req, ctx):
        with self.db_fn() as db:
            db.execute('DELETE FROM tasks WHERE user = ?', (ctx.openid,))
        return co.Empty()

    @requires_token
    @log_request
    def MarkAllAsRead(self, req, ctx):
        with self.db_fn() as db:
            db.execute('UPDATE notifications SET has_read = 1 WHERE user = ?', (ctx.openid,))
        return co.Empty()

    @log_request
    def GetSnapshot(self, request, context):
        snapshot_id = request.id

        # Check if snapshot is reported
        with self.db_fn() as db:
            r = db.execute('SELECT reported, report_reason FROM snapshots WHERE uuid = ?', (snapshot_id,)).fetchone()
            if r['reported'] > 0:
                context.abort(StatusCode.PERMISSION_DENIED, f"Snapshot is reported: {r['report_reason']}")

        with self.db_fn() as db:
            r = db.execute(
                "SELECT type, access_url, ledger_key, hash FROM data WHERE snapshot = ?", (snapshot_id,)
            )
        if r is None:
            context.abort(StatusCode.NOT_FOUND, "Snapshot not found")

        contents = []
        for datum in r:
            contents.append(co.Content(type=datum['type'], data=datum['access_url'].encode(), ledger_key=datum['ledger_key'], hash=datum['hash']))
        return sc.Contents(contents=contents)

    @log_request
    def ListSnapshots(self, request, context):
        url = request.url

        snapshots = []
        with self.db_fn() as db:
            for r in db.execute(
                "SELECT uuid, url, timestamp FROM snapshots WHERE snapshots.url = ?",
                (url,),
            ):
                snapshots.append(
                    co.Snapshot(
                        id=r["uuid"],
                        url=r["url"],
                        timestamp=r["timestamp"],
                    )
                )
        return sc.Snapshots(snapshots=snapshots)

    @log_request
    def Report(self, req, ctx):
        with self.db_fn() as db:
            db.execute('UPDATE snapshots SET reported = 1, report_reason = ? WHERE uuid = ?', (req.reason, req.snapshot_id))

    @log_request
    def RegisterWorker(self, request, context):
        addr = request.addr
        port = request.port
        self.workers.add(f"{addr}:{port}")
        return co.Empty()


def serve(port=8000):
    server = grpc.server(ThreadPoolExecutor(max_workers=10))
    my_scheduler = MyScheduler(
        db_fn=lambda: open_db(Path("db/production"), Path("db/schema")),
        auth=Auth(),
        task_event=Event(),
    )
    add_SchedulerServicer_to_server(my_scheduler, server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    logging.info(f"Scheduler listening at 0.0.0.0:{port}")
    server.wait_for_termination()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = os.getenv("SCHEDULER_PORT", 8848)
    serve(port)
