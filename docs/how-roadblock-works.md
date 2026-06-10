# How Roadblock Works

This document explains how crucible's roadblock synchronization
system operates — the distributed barrier and message passing
mechanism that coordinates all engines during a benchmark run.

For how engines participate in roadblock synchronization, see
[how-engines-work.md](how-engines-work.md).
For the benchmark execution phases that use roadblock, see
[how-benchmark-execution-works.md](how-benchmark-execution-works.md).

## Overview

Roadblock is a barrier synchronization system that coordinates
distributed benchmark engines. It solves a fundamental problem:
when multiple engines (clients, servers, profilers) run across
different hosts, pods, or VMs, they need to execute in lockstep
— servers must start before clients connect, all clients must
finish before data is collected, tools must start before any
benchmark activity begins.

Roadblock provides two capabilities:

- **Barrier synchronization**: All participants must reach a
  named synchronization point before anyone proceeds
- **Message passing**: Participants can exchange JSON messages
  during synchronization, enabling service discovery, timeout
  negotiation, and environment injection

Roadblock uses Valkey (Redis-compatible) as its backing store,
communicating via Redis streams.

## The leader-follower model

Each roadblock synchronization has exactly one **leader** and
one or more **followers**:

- **Leader**: rickshaw-run.py running on the controller. It
  orchestrates the run and decides when to proceed or abort.
- **Followers**: Engine processes and endpoint processes.
  Engines run inside containers, pods, or chroots on
  distributed hosts. Endpoints (the scripts that manage
  engine deployment on each target) also participate as
  followers — they need to synchronize with the engines
  they deploy, particularly during the deployment phase,
  per-sample service creation/teardown (endpoint-start/stop),
  and data transfer.

Each synchronization point is identified by a UUID-based name:

```
{run-id}:{phase-name}
```

For per-sample phases, the name includes the test context:

```
{run-id}:{iteration}-{sample}-{attempt}:{phase-name}
```

The leader knows exactly which followers to expect at each
barrier. If a follower doesn't arrive within the timeout, the
barrier fails.

## Barrier protocol

Each barrier goes through a series of phases:

### 1. Online

Participants announce their presence:

- Followers send `follower-online` to the leader
- Leader sends `leader-online` to followers
- When all expected followers are present, leader broadcasts
  `all-online`

### 2. Ready

Followers signal they've completed their pre-barrier work:

- **Normal**: Follower sends `follower-ready` — work is done,
  ready to proceed
- **Abort**: Follower sends `follower-ready-abort` — an error
  occurred, requesting the run abort this sample
- **Waiting**: Follower sends `follower-ready-waiting` — a
  long-running operation is in progress (see
  [wait-for mechanism](#the-wait-for-mechanism))

### 3. Decision

The leader evaluates the collected state:

- **All ready, no aborts**: Sends `all-go` — everyone proceeds
- **Abort detected**: Sends `all-abort` — sample is failed,
  engines should clean up
- **Wait-for active**: Sends `all-wait` — switches to heartbeat
  monitoring mode

### 4. Heartbeat (when wait-for is active)

For barriers where followers are running long operations:

- Leader sends `leader-heartbeat` periodically
- Each follower responds with `follower-heartbeat`
- When a follower's operation completes, it sends
  `follower-waiting-complete`
- When all complete: leader sends `all-go`
- If a follower stops responding: `heartbeat-timeout` and abort

### 5. Exit

Participants acknowledge completion:

- Followers send `follower-gone`
- Leader sends `all-gone` and cleans up Redis keys

## Message passing

Roadblock's message passing allows engines to communicate
during synchronization without direct network connections
between them.

### How it works

1. **Before the barrier**: An engine writes JSON messages to
   its `msgs/tx/` directory
2. **At the barrier**: The engine's roadblock invocation
   includes `--user-messages <file>` pointing to the queued
   messages
3. **During synchronization**: Messages are transmitted via
   Redis streams and delivered to recipients
4. **After the barrier**: Received messages are logged and
   parsed from the roadblock output

### Message format

```json
{
    "recipient": {
        "type": "all",
        "id": "all"
    },
    "user-object": {
        "svc": {
            "ip": "10.244.1.5",
            "ports": [30000]
        }
    }
}
```

Messages specify a recipient (leader, follower, or all) and
carry a JSON payload. The payload is application-defined —
roadblock delivers it without interpreting it.

### Common use cases

- **Service discovery**: Servers publish their IP and ports
  so clients know where to connect
- **Timeout adjustment**: Client engine 1 discovers the
  benchmark runtime and sends a timeout message so the leader
  adjusts the next barrier's timeout
- **Environment injection**: Endpoints send deployment context
  (node name, userenv, osruntime) to engines during engine-init
- **Endpoint service modification**: Kubernetes endpoint sends
  Service ClusterIP/NodePort to replace raw pod IPs

## The synchronization sequence in a run

A complete benchmark run includes these barriers (each with a
begin and end pair):

### Deployment and initialization

```
endpoint-pre-deploy-begin/end    — endpoint prepares
endpoint-deploy-begin/end        — engines are created
engine-init-begin/end            — engines initialize, receive env vars
get-data-begin/end               — engines fetch scripts and commands
collect-sysinfo-begin/end        — system profiling (packrat)
start-tools-begin/end            — tool collectors launch
```

### Per-sample execution (repeated for each iteration × sample)

```
{test}:infra-start-begin/end     — infrastructure setup (e.g., TRex service)
{test}:server-start-begin/end    — server processes launch, publish service info
{test}:endpoint-start-begin/end  — endpoint configures networking (e.g., K8s
                                   Service creation, NodePort/LoadBalancer setup,
                                   service info transformation)
{test}:client-start-begin/end    — benchmark execution (runtime discovery, workload)
{test}:client-stop-begin/end     — clients finish
{test}:endpoint-stop-begin/end   — endpoint tears down networking (e.g., K8s
                                   Service deletion)
{test}:server-stop-begin/end     — servers shut down
{test}:infra-stop-begin/end      — infrastructure teardown
```

Where `{test}` is `{iteration}-{sample}-{attempt}`.

### Cleanup

```
stop-tools-begin/end             — tool collectors shut down
send-data-begin/end              — data archived and transferred
```

## Timeout and failure handling

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Successful synchronization |
| 3 | Timeout — a follower didn't arrive |
| 4 | Abort — an engine requested abort |
| 5 | Heartbeat timeout — wait-for follower stopped responding |

### Timeout (exit code 3)

A follower didn't reach the barrier within the allowed time.
This is treated as a severe failure — the entire run is
terminated because an unresponsive engine indicates a
fundamental problem (crashed process, network failure, hung
workload).

### Abort (exit code 4)

An engine detected an error and sent `follower-ready-abort`.
The leader propagates this as `all-abort` to all participants.
The current sample is marked as failed, but the run continues
to the next sample or iteration (up to `max-sample-failures`).

Abort is the normal failure path — it handles expected error
conditions like a benchmark script returning non-zero, a
prerequisite check failing, or a connection error.

### Follower management

The leader tracks which followers are active. When a follower
fails or is dropped (due to abort or timeout), it's removed
from the active follower list. Subsequent barriers only expect
the remaining active followers, preventing cascading timeouts
from an already-failed engine.

## The wait-for mechanism

Some operations have unpredictable duration — the leader can't
set a reasonable fixed timeout. The wait-for mechanism handles
this by switching from fixed-timeout barriers to heartbeat-
monitored barriers.

### How it works

1. The follower specifies `--wait-for "<command>"` when
   entering the barrier
2. The command runs as a subprocess while the follower
   participates in the barrier
3. The follower sends `follower-ready-waiting` instead of
   `follower-ready`
4. The leader detects waiting followers and sends `all-wait`
5. The leader begins periodic heartbeat exchanges to verify
   followers are still alive
6. When the wait-for command completes, the follower sends
   `follower-waiting-complete`
7. Once all followers complete, the leader sends `all-go`

### Where wait-for is used

Wait-for is used in several contexts throughout a run:

- **Unbounded benchmark workloads** (`client-start`): When a
  benchmark's `get-runtime` script returns `-1` (unknown
  duration), the client barrier uses wait-for to let the
  benchmark run until it naturally completes
- **Tool shutdown** (`stop-tools-end`): Tool stop scripts may
  need significant time to compress collected data (e.g.,
  xz-compressing large trace files). Wait-for lets each engine
  take as long as it needs
- **Data archival** (`send-data-end`): Archiving the engine's
  working directory and transferring it via SSH takes variable
  time depending on data volume and network speed

The common thread: any operation where the duration is
unpredictable and engines may finish at different times.
Wait-for allows each engine to proceed at its own pace while
the leader monitors liveness.

## Valkey backend

Roadblock uses Valkey (Redis-compatible) as its communication
backbone. All barrier state, participant tracking, and message
passing flows through Redis streams.

### How it's managed

Crucible starts the Valkey server as a container
(`crucible-valkey`) alongside the other services at the
beginning of a run. It runs on port 6379.

The Valkey server is shared across all barriers in a run.
Redis stream keys are namespaced by the run UUID, so
concurrent runs don't interfere with each other.

### Password management

Valkey uses password authentication. The password is
generated when the Valkey service instance starts and
persists for the lifetime of that instance:

1. When Valkey is started (if not already running),
   crucible generates a random password by hashing 4KB
   of `/dev/urandom` with SHA-256
2. The password is written to `config/valkey_pass`
3. Valkey starts with `--requirepass` using this password
4. For each run, the password is read from `config/valkey_pass`
   and passed to rickshaw-run.py via `--roadblock-password`,
   which distributes it to all engines and endpoints
5. When Valkey is stopped, the password file is deleted

The password is tied to the service instance, not to
individual runs. Multiple runs can share the same Valkey
instance (and password) as long as the service remains
running. Each run's roadblock barriers are isolated by
their unique run UUID, not by authentication.

### Why Redis streams

Redis streams provide ordered, persistent message delivery
with consumer group semantics. This ensures:

- Messages are delivered in order
- Messages persist until consumed (survives brief network
  interruptions)
- Multiple consumers can read the same stream
- Streams are automatically cleaned up when the barrier
  completes

## Configuration

### Timeout settings

Timeouts are configured in `rickshaw-settings.json`:

```json
{
    "roadblock": {
        "timeouts": {
            "default": 240,
            "endpoint-deploy": 1440,
            "collect-sysinfo": 1200,
            "engine-start": 1440
        }
    }
}
```

- **default** (240s / 4 min): Most synchronization points
- **endpoint-deploy** (1440s / 24 min): Engine creation
  (image pulling, pod scheduling)
- **collect-sysinfo** (1200s / 20 min): System profiling
- **engine-start** (1440s / 24 min): Engine bootstrap

These are base values. Dynamic timeout adjustment happens
for the `client-start` barrier when the benchmark's runtime
is discovered — the timeout is set to the reported runtime
plus padding.

### Connection watchdog

An optional connection watchdog can be enabled to monitor
the Redis connection health. When enabled, a background
thread pings Redis every second and logs any connection
failures.

```json
{
    "roadblock": {
        "connection-watchdog": true
    }
}
```

This is primarily a debugging tool for diagnosing
intermittent connectivity issues between engines and the
Valkey server.
