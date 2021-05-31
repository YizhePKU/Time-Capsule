from scheduler.auth import Auth, AuthException
import random
import threading
import scheduler.utils as utils
import pytest


def start_thread(fn):
    th = threading.Thread(target=fn)
    th.start()
    return th


def fail(fn):
    def inner():
        with pytest.raises(AuthException):
            fn()

    return inner


@pytest.fixture
def auth():
    auth = Auth(timeout=30)
    yield auth
    auth.shutdown()


@pytest.fixture
def token():
    return utils.uuid()


@pytest.fixture
def openid():
    return utils.uuid()


def test_success(auth, token, openid):
    start_thread(lambda: auth.request_login(token))
    auth.confirm_login(token, openid)
    assert auth.get_openid(token) == openid


def test_duplicate_token(auth, token):
    start_thread(lambda: auth.request_login(token))
    start_thread(fail(lambda: auth.request_login(token)))


def test_bad_confirm(auth, token, openid):
    start_thread(fail(lambda: auth.confirm_login(token, openid)))


def test_bad_access(auth, token):
    start_thread(fail(lambda: auth.get_openid(token)))


def test_multithread(auth):
    N = 32
    tokens = [utils.uuid() for _ in range(N)]
    openids = [utils.uuid() for _ in range(N)]
    threads = []

    for token in tokens:
        th = start_thread(lambda: auth.request_login(token))
        threads.append(th)

    random.shuffle(tokens)

    for token, openid in zip(tokens, openids):
        th = start_thread(lambda: auth.confirm_login(token, openid))
        threads.append(th)

    for th in threads:
        th.join()
    assert len(auth.pending_tokens) == 0
    assert len(auth.confirmed_tokens) == N

    for token, openid in zip(tokens, openids):
        assert auth.get_openid(token) == openid
