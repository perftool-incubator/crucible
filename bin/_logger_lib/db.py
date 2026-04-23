# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

import sqlite3
import time
from pathlib import Path


SCHEMA_VERSION = 1

INIT_SQL = """
CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY NOT NULL,
    stream TEXT UNIQUE NOT NULL
);

INSERT OR IGNORE INTO streams (id, stream) VALUES (1, 'STDOUT');
INSERT OR IGNORE INTO streams (id, stream) VALUES (2, 'STDERR');

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    source TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    command TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    session_id TEXT UNIQUE NOT NULL,
    timestamp REAL NOT NULL,
    source INTEGER NOT NULL REFERENCES sources (id),
    command INTEGER NOT NULL REFERENCES commands (id)
);

CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session INTEGER NOT NULL REFERENCES sessions (id),
    timestamp REAL NOT NULL,
    stream INTEGER NOT NULL REFERENCES streams (id),
    line TEXT
);

CREATE TABLE IF NOT EXISTS db_state (
    timestamp REAL PRIMARY KEY NOT NULL
);
"""

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY NOT NULL);
CREATE INDEX IF NOT EXISTS idx_lines_session_timestamp ON lines (session, timestamp);
CREATE INDEX IF NOT EXISTS idx_lines_stream ON lines (stream);
CREATE INDEX IF NOT EXISTS idx_sessions_timestamp ON sessions (timestamp);
"""


def init_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(INIT_SQL)
    conn.execute("INSERT OR REPLACE INTO db_state (timestamp) VALUES (?)", (time.time(),))
    conn.commit()
    conn.close()


def _run_migrations(conn):
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row[0] if row else 0
    except sqlite3.OperationalError:
        current = 0

    if current < SCHEMA_VERSION:
        conn.executescript(MIGRATION_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()


def verify_db(db_path):
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        conn.execute("SELECT timestamp FROM db_state")
    except sqlite3.OperationalError:
        conn.close()
        raise RuntimeError(f"SQLite log DB '{db_path}' does not appear to be initialized")
    return conn


def _get_or_create(conn, table, column, value):
    row = conn.execute(
        f"SELECT id FROM {table} WHERE {column} = ?", (value,)
    ).fetchone()
    if row:
        return row[0]
    cursor = conn.execute(
        f"INSERT INTO {table} ({column}) VALUES (?)", (value,)
    )
    conn.commit()
    return cursor.lastrowid


def setup_session(conn, source, session_id, command):
    ts = time.time()
    conn.execute("UPDATE db_state SET timestamp = ?", (ts,))
    conn.commit()

    source_id = _get_or_create(conn, "sources", "source", source)
    command_id = _get_or_create(conn, "commands", "command", command)

    conn.execute(
        "INSERT INTO sessions (session_id, timestamp, source, command) "
        "VALUES (?, ?, ?, ?)",
        (session_id, ts, source_id, command_id),
    )
    conn.commit()

    row = conn.execute(
        "SELECT id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0]


class LogInserter:
    def __init__(self, conn, session_id):
        self.conn = conn
        self.session_id = session_id
        self.in_transaction = False

    def begin(self):
        if not self.in_transaction:
            self.conn.execute("BEGIN")
            self.in_transaction = True

    def insert(self, timestamp, stream_name, message):
        self.begin()
        self.conn.execute(
            "INSERT INTO lines (session, timestamp, stream, line) "
            "SELECT ?, ?, id, ? FROM streams WHERE stream = ?",
            (self.session_id, timestamp, message, stream_name),
        )

    def commit(self):
        if self.in_transaction:
            self.conn.commit()
            self.in_transaction = False

    def rollback(self):
        if self.in_transaction:
            self.conn.rollback()
            self.in_transaction = False

    def close(self):
        self.commit()
        self.conn.close()
