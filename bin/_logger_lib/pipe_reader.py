# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

import os
import select
import threading
import time


PIPE_FLUSH_STR = "CRUCIBLE_PIPE_FLUSH"
CLOSE_PIPE_STR = "CRUCIBLE_CLOSE_LOG_PIPE"


def pipe_reader(stdout_pipe, stderr_pipe, msg_queue, flush_event, shutdown_event):
    stdout_fh = open(stdout_pipe, "r")
    stderr_fh = open(stderr_pipe, "r")

    fd_map = {
        stdout_fh.fileno(): (stdout_fh, "STDOUT", 0),
        stderr_fh.fileno(): (stderr_fh, "STDERR", 1),
    }
    open_fds = set(fd_map.keys())

    while open_fds:
        readable, _, _ = select.select(list(open_fds), [], [], 0.1)

        if not readable:
            flush_event.set()
            continue

        for fd in readable:
            fh, stream, stream_id = fd_map[fd]
            line = fh.readline()

            if not line:
                open_fds.discard(fd)
                fh.close()
                continue

            ts = time.time()
            line = line.rstrip("\n").rstrip("\r")

            if PIPE_FLUSH_STR in line:
                continue

            if CLOSE_PIPE_STR in line:
                open_fds.discard(fd)
                fh.close()
                continue

            msg_queue.put((ts, stream, stream_id, line))

    msg_queue.put(None)
    shutdown_event.set()


def flusher(stdout_pipe, stderr_pipe, flush_event, shutdown_event):
    stdout_flush = f"STDOUT->{PIPE_FLUSH_STR}\n"
    stderr_flush = f"STDERR->{PIPE_FLUSH_STR}\n"

    while not shutdown_event.is_set():
        if flush_event.wait(timeout=1.0):
            flush_event.clear()

            if shutdown_event.is_set():
                break

            for pipe_path, flush_str in [
                (stdout_pipe, stdout_flush),
                (stderr_pipe, stderr_flush),
            ]:
                try:
                    fd = os.open(pipe_path, os.O_WRONLY | os.O_NONBLOCK)
                    os.write(fd, flush_str.encode())
                    os.close(fd)
                except OSError:
                    pass
