import time
import uuid as uuidlib
import hashlib
import sqlite3
from pathlib import Path


def unix_time():
    return int(time.time())


def uuid():
    return uuidlib.uuid4().bytes


def sha256(data: bytes):
    return hashlib.sha256(data).digest()


def open_db(db: Path, schema: Path):
    # Initialize database if necessary
    if not db.exists():
        db.touch()
        conn = sqlite3.connect(db)
        with open(schema) as file:
            schema = file.read()
            conn.executescript(schema)
    else:
        conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn
