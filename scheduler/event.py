import threading


class Event:
    def __init__(self):
        self.listeners = set()

    def register(self):
        sem = threading.Semaphore(0)
        self.listeners.add(sem)
        return sem

    def unregister(self, sem):
        self.listeners.remove(sem)

    def notify(self):
        for sem in self.listeners:
            sem.release()
