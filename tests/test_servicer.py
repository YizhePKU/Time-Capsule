import pytest
import grpc
from time import sleep
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from scheduler.auth import Auth
from scheduler.event import Event
from scheduler.servicer import MyScheduler
from scheduler.utils import open_db, uuid

from capsule import common_pb2 as co
from capsule import scheduler_pb2 as sc
from capsule import worker_pb2 as wo
from capsule import storage_pb2 as st


token = "mytoken"
openid = "myopenid"
username = "yizhe"


def wait_until(fn, interval=0.1, timeout=5):
    for i in range(round(timeout / interval)):
        if fn():
            return True
        else:
            sleep(interval)
    return False


@pytest.fixture
def pool():
    return ThreadPoolExecutor(max_workers=4)


@pytest.fixture
def db_fn(tmp_path):
    db_path = tmp_path / "test.db"
    return lambda: open_db(db_path, Path("db/schema"))


@pytest.fixture
def servicer(db_fn):
    return MyScheduler(auth=Auth(), task_event=Event(), db_fn=db_fn)


class RpcException(Exception):
    pass


class Ctx:
    def __init__(self):
        self.metadata = {}

    def invocation_metadata(self):
        return self.metadata

    def abort(code, msg):
        raise RpcException(f"Code {code}: {msg}")


@pytest.fixture
def ctx():
    ctx = Ctx()
    ctx.metadata["token"] = token
    return ctx


@pytest.fixture
def login(servicer, ctx, pool):
    pool.submit(lambda: servicer.TryLogin(co.LoginRequest(token=token), ctx))
    servicer.ConfirmLogin(
        co.LoginConfirmation(token=token, openid=openid, name=username), ctx
    )


def test_login(servicer, ctx, login):
    r = servicer.GetUserData(co.Empty(), ctx)
    assert r.username == username


def test_active_tasks(servicer, ctx, login, pool):
    def add_task():
        with servicer.db_fn() as db:
            params = {
                'id': uuid(),
                'user': openid,
                'url': 'http://example.com',
                'status': co.Task.Status.failed,
                'article_id': uuid(),
            }
            db.execute('INSERT INTO tasks VALUES ($id, $user, $url, $status, $article_id)', params)

    results = []
    canceled = False

    def inner():
        for tasks in servicer.GetActiveTasks(co.Empty(), ctx):
            if canceled:
                return
            results.append(tasks.tasks)

    add_task()
    pool.submit(inner)
    assert wait_until(lambda: len(results) == 1)

    add_task()
    servicer.task_event.notify()
    assert wait_until(lambda: len(results) == 2)

    add_task()
    servicer.task_event.notify()
    assert wait_until(lambda: len(results) == 3)

    # Cancel the stream.
    # In production the client will call stream.cancel and the thread will be returned to the thread pool.
    # See https://github.com/grpc/grpc/issues/22874.
    canceled = True
    sem = servicer.task_event.listeners.pop()
    sem.release()


def test_capture(servicer, ctx, login, pool, monkeypatch):
    class WorkerStub:
        def Crawl(self, req):
            for url in req.urls:
                if not 'fail' in url:
                    yield wo.CrawlResponse(url=url)

    class StorageStub:
        def StoreContent(self, req):
            pass

    monkeypatch.setattr(servicer, 'worker_stub', lambda: WorkerStub())
    monkeypatch.setattr(servicer, 'storage_stub', lambda: StorageStub())

    results = []
    canceled = False

    def inner():
        for tasks in servicer.GetActiveTasks(co.Empty(), ctx):
            if canceled:
                return
            results.append(tasks.tasks)

    pool.submit(inner)
    assert wait_until(lambda: len(results) == 1)

    servicer.Capture(sc.CaptureRequest(article_id=uuid(), urls=['url1', 'url2', 'url3_fail', 'url4']), ctx)
    assert wait_until(lambda: len(results) == 6)

    tasks = results[5]
    print(results[4:])
    assert len(tasks) == 4
    assert all(task.status == co.Task.Status.failed for task in tasks if 'fail' in task.url)
    assert all(task.status == co.Task.Status.finished for task in tasks if 'fail' not in task.url)

    canceled = True
    sem = servicer.task_event.listeners.pop()
    sem.release()
