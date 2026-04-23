#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

import os
import queue
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _logger_lib.db import verify_db, setup_session, LogInserter, _run_migrations
from _logger_lib.output_writer import write_line
from _logger_lib.pipe_reader import pipe_reader, flusher

signal.signal(signal.SIGINT, lambda s, f: None)

sys.stdout.reconfigure(line_buffering=False)
sys.stderr.reconfigure(line_buffering=False)


def db_writer_and_output(msg_queue, inserter):
    BATCH_WINDOW = 0.010

    while True:
        batch = []
        deadline = time.time() + BATCH_WINDOW

        while time.time() < deadline:
            try:
                msg = msg_queue.get(timeout=0.005)
                if msg is None:
                    batch.sort(key=lambda m: m[0])
                    for ts, stream, stream_id, line in batch:
                        write_line(stream_id, line)
                        inserter.insert(ts, stream, line)
                    inserter.commit()
                    return
                batch.append(msg)
            except queue.Empty:
                break

        if batch:
            batch.sort(key=lambda m: m[0])
            for ts, stream, stream_id, line in batch:
                write_line(stream_id, line)
                inserter.insert(ts, stream, line)
            inserter.commit()
        else:
            time.sleep(0.05)


def main():
    if len(sys.argv) < 7:
        print(f"Usage: {sys.argv[0]} <source> <session_id> <command> <log_db> <stdout_pipe> <stderr_pipe>",
              file=sys.stderr)
        sys.exit(1)

    source = sys.argv[1]
    session_id = sys.argv[2].strip('"')
    command = sys.argv[3].replace("___", " ").strip('"')
    log_db = sys.argv[4]
    stdout_pipe = sys.argv[5]
    stderr_pipe = sys.argv[6]

    try:
        conn = verify_db(log_db)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    _run_migrations(conn)
    db_session_id = setup_session(conn, source, session_id, command)
    inserter = LogInserter(conn, db_session_id)

    msg_queue = queue.Queue()
    flush_event = threading.Event()
    shutdown_event = threading.Event()

    reader_thread = threading.Thread(
        target=pipe_reader,
        args=(stdout_pipe, stderr_pipe, msg_queue, flush_event, shutdown_event),
        daemon=True,
    )
    flusher_thread = threading.Thread(
        target=flusher,
        args=(stdout_pipe, stderr_pipe, flush_event, shutdown_event),
        daemon=True,
    )
    writer_thread = threading.Thread(
        target=db_writer_and_output,
        args=(msg_queue, inserter),
    )

    reader_thread.start()
    flusher_thread.start()
    writer_thread.start()

    writer_thread.join()
    reader_thread.join()
    shutdown_event.set()
    flusher_thread.join()

    inserter.close()

    try:
        os.unlink(stdout_pipe)
    except OSError:
        pass
    try:
        os.unlink(stderr_pipe)
    except OSError:
        pass


if __name__ == "__main__":
    main()
