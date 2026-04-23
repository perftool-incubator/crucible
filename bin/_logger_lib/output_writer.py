# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

import sys


def write_line(stream_id, message):
    target = sys.stdout if stream_id == 0 else sys.stderr
    target.buffer.write((message + "\r\n").encode())
    target.buffer.flush()
