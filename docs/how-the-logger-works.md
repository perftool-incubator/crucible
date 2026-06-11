# How the Logger Works

This document explains how crucible's logging system captures
and stores command output for later review.

## Overview

Every crucible command's stdout and stderr is automatically
captured to a SQLite database. This means you can review the
output from any past command — even ones that ran days ago —
without needing to manually redirect to log files.

The logger works transparently: output still appears on the
terminal in real time, but it's simultaneously recorded with
timestamps, stream identification (stdout vs stderr), and
session metadata. Each command invocation gets a unique
session ID, making it easy to isolate and review specific
runs.

## How it works

The logger uses a pipe-based architecture with a
containerized logger process:

### Startup

When you run any crucible command:

1. The `crucible` script generates a unique session ID (UUID)
2. Creates two named pipes (FIFOs) — one for stdout, one
   for stderr
3. Spawns a logger container that reads from both pipes
4. Redirects the main process's stdout and stderr to the
   pipes
5. Runs the actual command (via `_main`)

### During execution

Three threads run inside the logger container:

- **Pipe reader**: Reads lines from both named pipes using
  non-blocking I/O. Each line is timestamped and placed in
  a thread-safe queue.
- **Flusher**: Periodically writes markers to the pipes to
  ensure timely processing even during quiet periods.
- **Writer**: Batches lines from the queue, inserts them
  into the SQLite database in a single transaction, and
  echoes them back to the terminal.

Output appears on your terminal with no visible delay —
the pipe adds negligible latency.

### Shutdown

When the command finishes:

1. Close markers are sent through both pipes
2. The logger drains any remaining buffered output
3. Final database commit
4. Logger container exits
5. Named pipes are cleaned up

## The log database

All log data is stored in a SQLite database at
`~/.crucible/log.db`.

### What's recorded

For each command invocation:

- **Session**: UUID, timestamp, source ("console"), and
  the full command string
- **Lines**: Every line of output with millisecond-precision
  timestamps and stream identification (stdout or stderr)

### Database structure

```
sessions table:
  session_id  │  timestamp  │  source   │  command
  ────────────┼─────────────┼───────────┼──────────────────────
  6afd39a8... │  1718012345 │  console  │  crucible run foo.json

lines table:
  session  │  timestamp     │  stream  │  line
  ─────────┼────────────────┼──────────┼─────────────────────────
  1        │  1718012345.12 │  STDOUT  │  Starting benchmark...
  1        │  1718012345.45 │  STDERR  │  WARNING: low memory
  1        │  1718012346.78 │  STDOUT  │  Benchmark complete
```

The millisecond timestamps enable precise ordering of
interleaved stdout and stderr lines.

## Viewing logs

### View a session's output

```bash
crucible log view              # most recent session
crucible log view last         # same as above
crucible log view first        # oldest session
crucible log view sessionid <uuid>  # specific session
```

### Filtering options

```bash
crucible log view --grep "ERROR"        # lines matching pattern
crucible log view --stream stderr       # stderr only
crucible log view --tail 20             # last 20 lines
crucible log view --head 50             # first 50 lines
crucible log view --since 1h            # last hour
crucible log view --since 2d            # last 2 days
crucible log view --since 2026-06-10    # since specific date
crucible log view --color               # ANSI colors (stderr in red)
```

Options can be combined:

```bash
crucible log view --grep "ERROR" --since 1h --color
```

### Follow mode

```bash
crucible log view --follow
```

Continuously displays new log lines as they're written,
similar to `tail -f`. Useful for monitoring a long-running
`crucible run` from another terminal.

### List sessions

```bash
crucible log sessions
```

Shows a table of all recorded sessions:

```
Timestamp                  Session ID      Duration  Command
2026-06-10 10:45:23.123    6afd39a8...     1m23s     crucible run foo.json
2026-06-10 11:02:45.456    b3c7e912...     0m02s     crucible repo info
2026-06-10 11:15:00.789    d4e8f123...     45m12s    crucible run bar.json
```

### Database info

```bash
crucible log info
```

Shows database statistics: file size, total sessions, total
lines, and date range.

## Log management

The log database grows over time as more commands are
executed. There is no automatic retention policy.

### Maintenance commands

```bash
crucible log clear    # delete all sessions and lines
crucible log tidy     # VACUUM to reclaim disk space
```

`crucible log clear` removes all data. `crucible log tidy`
compacts the database file after deletions — SQLite doesn't
automatically shrink the file when rows are deleted, so
`tidy` reclaims that space.

### Typical growth

The database grows proportionally to the volume of command
output. A typical `crucible run` produces thousands of lines;
simple commands like `crucible repo info` produce a handful.
Over time, the database may grow to tens or hundreds of
megabytes. Periodic `crucible log clear` or `crucible log
tidy` keeps the size manageable.

### Database location

The database lives at `~/.crucible/log.db`, making it
per-user. Each user on a shared system has their own log
history.

## Why the logger runs in a container

The logger process runs inside a controller container rather
than directly on the host. This is consistent with crucible's
overall pattern: the host system has minimal dependencies
(just podman and git), and all crucible functionality runs
inside the controller image where Python, SQLite, and other
dependencies are guaranteed to be available.

This means logging works identically regardless of the host
OS or what's installed on it — the controller image provides
a consistent environment.
