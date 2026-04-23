# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

import json
import re
import sqlite3
import time
from datetime import datetime


def format_ts(epoch):
    dt = datetime.fromtimestamp(epoch)
    ms = int((epoch - int(epoch)) * 1000)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{ms:03d}"


def view_sessions(conn, filter_cmd=None, filter_arg=None,
                  stream_filter=None, grep_pattern=None,
                  since=None, until=None, output_format="plain",
                  use_color=False, count_only=False, tail=None):
    conditions = []
    params = []

    if filter_cmd == "first":
        conditions.append(
            "sessions.timestamp = "
            "(SELECT s2.timestamp FROM sessions AS s2 ORDER BY s2.timestamp ASC LIMIT 1)"
        )
    elif filter_cmd == "last":
        conditions.append(
            "sessions.timestamp = "
            "(SELECT s2.timestamp FROM sessions AS s2 ORDER BY s2.timestamp DESC LIMIT 1)"
        )
    elif filter_cmd == "sessionid" and filter_arg:
        conditions.append("sessions.session_id = ?")
        params.append(filter_arg)

    if stream_filter:
        conditions.append("streams.stream = ?")
        params.append(stream_filter.upper())

    if since:
        conditions.append("lines.timestamp >= ?")
        params.append(since)

    if until:
        conditions.append("lines.timestamp <= ?")
        params.append(until)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    query = (
        "SELECT "
        "sessions.session_id AS session_id, "
        "sessions.timestamp AS session_timestamp, "
        "commands.command AS session_command, "
        "sources.source AS session_source, "
        "lines.timestamp AS line_timestamp, "
        "lines.line AS line, "
        "streams.stream AS line_stream "
        "FROM sessions "
        "JOIN lines ON sessions.id = lines.session "
        "JOIN streams ON streams.id = lines.stream "
        "JOIN sources ON sources.id = sessions.source "
        "JOIN commands ON commands.id = sessions.command "
        f"{where} "
        "ORDER BY sessions.timestamp, lines.timestamp"
    )

    cursor = conn.execute(query, params)

    if count_only:
        count = 0
        for row in cursor:
            line = row[5]
            if grep_pattern and not re.search(grep_pattern, line or ""):
                continue
            count += 1
        print(count)
        return

    # Pre-fetch session durations
    session_durations = {}
    for sid, dur in conn.execute(
        "SELECT sessions.session_id, MAX(lines.timestamp) - MIN(lines.timestamp) "
        "FROM sessions JOIN lines ON sessions.id = lines.session "
        "GROUP BY sessions.id"
    ).fetchall():
        session_durations[sid] = dur

    last_session_ts = -1
    sep = "=" * 94

    def _format_line(line_ts_fmt, line_stream, line):
        if use_color and line_stream == "STDERR":
            return f"\033[2m[{line_ts_fmt}]\033[0m[\033[31m{line_stream}\033[0m] \033[31m{line}\033[0m"
        elif use_color:
            return f"\033[2m[{line_ts_fmt}]\033[0m[{line_stream}] {line}"
        else:
            return f"[{line_ts_fmt}][{line_stream}] {line}"

    def _print_session_header(session_ts_fmt, session_id, session_cmd, session_src, dur_str):
        session_id = _strip_quotes(session_id)
        session_cmd = _strip_quotes(session_cmd)
        if use_color:
            print(f"\033[1;36m{sep}\033[0m")
            print(f"\033[1msession id:  {session_id}\033[0m")
        else:
            print(sep)
            print(f"session id:  {session_id}")
        print(f"command:     {session_cmd}")
        print(f"timestamp:   {session_ts_fmt}")
        print(f"duration:    {dur_str}")
        print(f"source:      {session_src}")
        print()

    pending_header = None
    pending_lines = []

    def _flush_pending():
        nonlocal pending_header, pending_lines
        if pending_header is None:
            return
        _print_session_header(*pending_header)
        lines_to_print = pending_lines[-tail:] if tail else pending_lines
        for l in lines_to_print:
            print(l)
        pending_header = None
        pending_lines = []

    for row in cursor:
        session_id = row[0]
        session_ts = row[1]
        session_cmd = row[2]
        session_src = row[3]
        line_ts = row[4]
        line = row[5] or ""
        line_stream = row[6]

        if grep_pattern and not re.search(grep_pattern, line):
            continue

        if output_format == "json":
            print(json.dumps({
                "session_id": session_id,
                "timestamp": line_ts,
                "stream": line_stream,
                "line": line,
            }))
            continue

        session_ts_fmt = format_ts(session_ts)
        line_ts_fmt = format_ts(line_ts)

        if session_ts != last_session_ts:
            _flush_pending()
            dur = session_durations.get(session_id)
            dur_str = _format_duration(dur) if dur is not None else "n/a"
            pending_header = (session_ts_fmt, session_id, session_cmd, session_src, dur_str)
            last_session_ts = session_ts

        formatted = _format_line(line_ts_fmt, line_stream, line)
        if tail:
            pending_lines.append(formatted)
        else:
            if pending_header:
                _print_session_header(*pending_header)
                pending_header = None
            print(formatted)

    _flush_pending()


def show_info(conn, db_path=None, output_format="plain"):
    session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    line_count = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]

    first_ts = conn.execute(
        "SELECT MIN(timestamp) FROM sessions"
    ).fetchone()[0]
    last_ts = conn.execute(
        "SELECT MAX(timestamp) FROM sessions"
    ).fetchone()[0]

    db_size = None
    if db_path:
        import os
        try:
            size_bytes = os.path.getsize(db_path)
            if size_bytes >= 1073741824:
                db_size = f"{size_bytes / 1073741824:.1f}G"
            elif size_bytes >= 1048576:
                db_size = f"{size_bytes / 1048576:.1f}M"
            elif size_bytes >= 1024:
                db_size = f"{size_bytes / 1024:.1f}K"
            else:
                db_size = f"{size_bytes}"
        except OSError:
            pass

    info = {
        "db_path": db_path,
        "db_size": db_size,
        "sessions": session_count,
        "lines": line_count,
        "first_session": format_ts(first_ts) if first_ts else None,
        "last_session": format_ts(last_ts) if last_ts else None,
    }

    if output_format == "json":
        print(json.dumps(info, indent=2))
    else:
        fmt = "%-25s %s"
        print("Crucible log information:\n")
        if db_path:
            print(fmt % ("Current Log:", db_path))
        if db_size:
            print(fmt % ("Log Size:", db_size))
        if first_ts:
            print(fmt % ("First session:", format_ts(first_ts)))
        if last_ts:
            print(fmt % ("Last session:", format_ts(last_ts)))
        print(fmt % ("Total sessions:", session_count))
        print()


def clear_db(conn):
    conn.execute("DELETE FROM lines")
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM sources")
    conn.execute("DELETE FROM commands")
    conn.commit()
    conn.execute("VACUUM")


def tidy_db(conn):
    conn.execute("VACUUM")


def _strip_quotes(s):
    if s and len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _format_duration(seconds):
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s:02d}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m:02d}m"


def list_sessions(conn, grep_pattern=None, output_format="plain",
                  use_color=False, sort_by="timestamp", sort_order="asc"):
    order_col = "commands.command" if sort_by == "command" else "sessions.timestamp"
    direction = "DESC" if sort_order == "desc" else "ASC"
    query = (
        "SELECT sessions.timestamp, sessions.session_id, commands.command, "
        "  (SELECT MAX(lines.timestamp) - MIN(lines.timestamp) "
        "   FROM lines WHERE lines.session = sessions.id) AS duration "
        "FROM sessions "
        "JOIN commands ON commands.id = sessions.command "
        f"ORDER BY {order_col} {direction}"
    )

    rows = conn.execute(query).fetchall()

    if output_format == "json":
        for ts, session_id, command, duration in rows:
            session_id = _strip_quotes(session_id)
            command = _strip_quotes(command)
            if grep_pattern and not re.search(grep_pattern, command):
                continue
            print(json.dumps({
                "timestamp": format_ts(ts),
                "session_id": session_id,
                "command": command,
                "duration": round(duration, 3) if duration else None,
            }))
        return

    max_id_len = max((len(_strip_quotes(r[1])) for r in rows), default=10)
    fmt = f"%-23s  %-{max_id_len}s  %-10s  %s"

    if use_color:
        print(f"\033[1m{fmt % ('Timestamp', 'Session ID', 'Duration', 'Command')}\033[0m")
    else:
        print(fmt % ("Timestamp", "Session ID", "Duration", "Command"))

    for ts, session_id, command, duration in rows:
        session_id = _strip_quotes(session_id)
        command = _strip_quotes(command)
        if grep_pattern and not re.search(grep_pattern, command):
            continue
        ts_fmt = format_ts(ts)
        dur_fmt = _format_duration(duration)
        if use_color:
            print(f"\033[2m{ts_fmt}\033[0m  {session_id}  {dur_fmt:<10}  {command}")
        else:
            print(fmt % (ts_fmt, session_id, dur_fmt, command))


def get_session_ids(conn):
    rows = conn.execute(
        "SELECT session_id FROM sessions ORDER BY timestamp"
    ).fetchall()
    for row in rows:
        print(row[0])
