import sqlite3
import sys
import unittest
from pathlib import Path


BIN_DIRECTORY = Path(__file__).resolve().parents[2] / "bin"
if str(BIN_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(BIN_DIRECTORY))

from _logger_lib.db import _run_migrations


class TestLoggerSchemaMigration(unittest.TestCase):
    def test_version_one_database_gets_bounded_context_index(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY NOT NULL);
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE lines (
                id INTEGER PRIMARY KEY,
                session INTEGER,
                timestamp REAL,
                stream INTEGER,
                line TEXT
            );
            CREATE TABLE sessions (timestamp REAL);
            """
        )

        _run_migrations(connection)
        _run_migrations(connection)

        self.assertEqual(
            connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
            2,
        )
        self.assertIsNotNone(
            connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'index' AND name = 'idx_lines_session_stream_id'"""
            ).fetchone()
        )

    def test_empty_schema_version_table_is_treated_as_version_zero(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY NOT NULL);
            CREATE TABLE lines (
                id INTEGER PRIMARY KEY,
                session INTEGER,
                timestamp REAL,
                stream INTEGER,
                line TEXT
            );
            CREATE TABLE sessions (timestamp REAL);
            """
        )

        _run_migrations(connection)

        self.assertEqual(
            connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
            2,
        )
        self.assertIsNotNone(
            connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'index' AND name = 'idx_lines_session_stream_id'"""
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
