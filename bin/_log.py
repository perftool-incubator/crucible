#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _logger_lib.db import init_db, verify_db, _run_migrations
from _logger_lib.viewer import (
    view_sessions, show_info, clear_db, tidy_db, get_session_ids,
    list_sessions,
)


def parse_datetime(s):
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    try:
        return float(s)
    except ValueError:
        print(f"ERROR: Cannot parse datetime: {s}", file=sys.stderr)
        sys.exit(1)


def main():
    if len(sys.argv) < 3:
        print("Usage: _log.py <mode> <log_db> [options]", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    log_db = sys.argv[2]

    if mode == "init":
        init_db(log_db)
        return

    if mode == "getsessionids":
        try:
            conn = verify_db(log_db)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        get_session_ids(conn)
        conn.close()
        return

    if mode == "sessions":
        try:
            conn = verify_db(log_db)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        parser = argparse.ArgumentParser(prog="_log.py sessions")
        parser.add_argument("--grep", default=None)
        parser.add_argument("--format", default="plain",
                            choices=["plain", "json"])
        parser.add_argument("--color", action="store_true", default=False)
        parser.add_argument("--no-color", action="store_true", default=False)
        parser.add_argument("--sort", default="timestamp",
                            choices=["timestamp", "command"])
        parser.add_argument("--order", default="asc",
                            choices=["asc", "desc"])

        args = parser.parse_args(sys.argv[3:])
        use_color = args.color and not args.no_color

        list_sessions(
            conn,
            grep_pattern=args.grep,
            output_format=args.format,
            use_color=use_color,
            sort_by=args.sort,
            sort_order=args.order,
        )
        conn.close()
        return

    if mode == "view":
        try:
            conn = verify_db(log_db)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        _run_migrations(conn)

        parser = argparse.ArgumentParser(prog="_log.py view")
        parser.add_argument("filter_cmd", nargs="?", default=None,
                            choices=["first", "last", "sessionid"])
        parser.add_argument("filter_arg", nargs="?", default=None)
        parser.add_argument("--stream", default=None,
                            choices=["stdout", "stderr"])
        parser.add_argument("--grep", default=None)
        parser.add_argument("--since", default=None)
        parser.add_argument("--until", default=None)
        parser.add_argument("--format", default="plain",
                            choices=["plain", "json"])
        parser.add_argument("--color", action="store_true", default=False)
        parser.add_argument("--no-color", action="store_true", default=False)
        parser.add_argument("--count", action="store_true", default=False)

        args = parser.parse_args(sys.argv[3:])

        use_color = args.color and not args.no_color

        since = parse_datetime(args.since) if args.since else None
        until = parse_datetime(args.until) if args.until else None

        view_sessions(
            conn,
            filter_cmd=args.filter_cmd,
            filter_arg=args.filter_arg,
            stream_filter=args.stream,
            grep_pattern=args.grep,
            since=since,
            until=until,
            output_format=args.format,
            use_color=use_color,
            count_only=args.count,
        )
        conn.close()
        return

    if mode == "info":
        try:
            conn = verify_db(log_db)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        fmt = "plain"
        if len(sys.argv) > 3 and sys.argv[3] == "--json":
            fmt = "json"
        show_info(conn, db_path=log_db, output_format=fmt)
        conn.close()
        return

    if mode == "clear":
        try:
            conn = verify_db(log_db)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        clear_db(conn)
        conn.close()
        return

    if mode == "tidy":
        try:
            conn = verify_db(log_db)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        tidy_db(conn)
        conn.close()
        return

    print(f"ERROR: Unknown mode: {mode}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
