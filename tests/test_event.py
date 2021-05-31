from time import sleep
import pytest
import threading

from scheduler.event import Event


@pytest.fixture
def event():
    return Event()


def test_empty_notify(event):
    event.notify()


def test_blocking(event):
    sem = event.register()

    def inner():
        assert sem.acquire(timeout=3)

    threading.Thread(target=inner).start()
    sleep(0.1)
    sem.release()


def test_unregister(event):
    sem = event.register()
    event.unregister(sem)
    assert len(event.listeners) == 0
