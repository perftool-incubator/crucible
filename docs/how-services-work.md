# How Services Work

This document explains how crucible manages its supporting
services — the infrastructure containers that provide
synchronization, result storage, result browsing, and container
image building.

For how the image-sourcing service builds images, see
[how-image-sourcing-works.md](how-image-sourcing-works.md).
For how roadblock uses the valkey service, see
[how-roadblock-works.md](how-roadblock-works.md).
For how the controller image contains these services, see
[how-the-controller-image-works.md](how-the-controller-image-works.md).
For how crucible captures command output, see
[how-the-logger-works.md](how-the-logger-works.md).

## Overview

Crucible runs several supporting services as podman containers.
These services provide the infrastructure that benchmark
execution depends on:

- **valkey** — Redis-compatible backend for roadblock
  synchronization
- **opensearch** — Result indexing and search engine
- **cdm-server** — CDM query API server for result analysis
- **httpd** — Web UI for browsing results and logs
- **image-sourcing** — Container image builder for engine images

Services are managed via `crucible start <service>` and
`crucible stop <service>`, and configured in
`config/services.json`. They run as long-lived detached
containers that persist across multiple benchmark runs.

## The services

### Valkey

Valkey is a Redis-compatible in-memory data store that serves
as the backend for roadblock synchronization. Every roadblock
barrier and message exchange flows through Valkey's Redis
streams. It runs on port 6379 with per-instance password
authentication.

An optional valkey monitor container can be enabled for
debugging. It runs the roadblock `redis-monitor.py` utility
to observe valkey traffic in real time.

### OpenSearch

OpenSearch stores and indexes benchmark results, enabling
search and analysis. It supports multiple instances for
different CDM versions — for example, a local instance for
the latest CDM format and a remote instance for an older
format.

OpenSearch runs with dynamically calculated JVM heap size
(half of system memory, capped at 32GB) and waits for
cluster health to reach at least "yellow" status before
accepting connections.

### CDM server

The CDM (Common Data Model) server provides a query API and
web-based dashboard for analyzing benchmark results stored in
OpenSearch. The dashboard is a React application that provides
search, comparison, and deep-dive views for exploring result
data across runs, iterations, and metrics. The CDM server
automatically starts when OpenSearch starts and stops when
OpenSearch stops. It connects to all configured query-from
OpenSearch instances.

### httpd

The Apache HTTP server provides a web UI for browsing
benchmark results, logs, and run artifacts. It serves files
from the crucible result directory (`/var/lib/crucible`) and
includes transparent decompression of `.xz` files for
in-browser viewing.

### Image-sourcing

The source-images-service builds container images for
benchmark and tool engines. It runs per-architecture instances
— one for each CPU architecture that endpoints require. The
native architecture runs locally; non-native architectures
require remote builder hosts.

### Remote archive storage

Remote archive storage enables uploading and downloading result
archives to centralized storage services via rclone. This supports
S3-compatible storage (including self-hosted services like rustfs
and MinIO) and other S3-compatible backends.

Unlike the other services listed above, remote archive storage
does not run as a long-lived container. Instead, rclone commands
execute inside the controller container on demand when archiving
or unarchiving results with the `--remote` flag.

Configuration is stored in the `remote-archive` section of
`services.json`. Each named remote specifies a backend type,
credential file path, and backend-specific settings. Credential
files are stored externally and referenced by absolute path —
the same pattern used by registry credentials in
`registries.json`. A `tls-verify` option (default `true`)
controls TLS certificate verification for each remote,
allowing connections to services with self-signed certificates.

Managed via `crucible archive config [add|remove|info|default]`.

## Service lifecycle

### Starting and stopping

```bash
crucible start httpd          # start a specific service
crucible stop opensearch      # stop a specific service
crucible start all            # start all services
crucible stop all             # stop all services
```

The `service_control()` function in `bin/base` manages the
lifecycle for all services.

### Start order

Services have implicit dependencies that affect startup
order:

1. **Valkey** starts first — required by roadblock, which is
   needed before any engine coordination
2. **Image-sourcing** starts next — needed to build engine
   images before a run
3. **OpenSearch** starts on demand — needed for result indexing
   after a run completes
4. **CDM server** auto-starts with OpenSearch — depends on
   OpenSearch being available
5. **httpd** starts on demand — needed for result browsing

During a benchmark run, `crucible run` automatically starts
valkey and image-sourcing. OpenSearch, CDM server, and httpd
are started later when results are ready for indexing.

### Health checks

Some services require readiness verification after starting:

- **Image-sourcing**: Polls `GET /api/v1/health` every second
  for up to 60 seconds, waiting for `"status": "healthy"`
- **OpenSearch**: Two-stage check — first waits for the HTTP
  endpoint to respond, then waits for cluster health to reach
  "yellow" or "green" status

### Run protection

Services cannot be stopped while a benchmark run is active.
The `service_control()` function checks for running
`crucible-rickshaw-run` containers and refuses to stop
services if any are found. This prevents accidentally
disrupting an in-progress run.

## Container management

### Naming convention

All service containers follow the naming pattern
`crucible-<service>`:

- `crucible-httpd`
- `crucible-opensearch`
- `crucible-valkey`
- `crucible-valkey-monitor` (optional)
- `crucible-cdm-server`
- `crucible-image-sourcing-<arch>` (e.g.,
  `crucible-image-sourcing-x86_64`)

### How they run

Service containers run in detached mode with journald
logging. They use host networking (`--net=host`), share the
host PID namespace, and mount the crucible installation and
result directories. This gives services full access to the
host's network stack and filesystem where needed.

### Port management

Each service listens on a configurable port. If the
configured port is already in use (by another process or a
previous crucible instance), the `find_available_port()`
function automatically searches for the next available port,
trying up to 100 sequential ports. If a different port is
used, `services.json` is updated with the actual port.

Firewall rules are managed automatically — ports are opened
when services start and closed when they stop.

## Configuration

All service configuration lives in `config/services.json`,
which is validated against `schema/services.json`. The schema
enforces valid port numbers, endpoint types, image-sourcing
structure, and OpenSearch instance fields. Changes made via
crucible commands are validated before being saved; manual
edits should be checked with `crucible repo config show` to
confirm validity.

### Structure

```json
{
    "httpd": {
        "port": 8080
    },
    "cdm-server": {
        "port": 3000
    },
    "valkey": {
        "monitor": {
            "enabled": false
        }
    },
    "opensearch": {
        "instances": [
            {
                "name": "local-v9",
                "host": "localhost:9200",
                "cdmver": "v9dev"
            }
        ],
        "index-to": "local-v9",
        "query-from": ["local-v9"]
    },
    "image-sourcing": {
        "use": true,
        "services": {
            "default": {
                "start": true,
                "location": {
                    "address": "localhost",
                    "port": 8888,
                    "protocol": "http"
                }
            }
        }
    }
}
```

The `remote-archive` section configures remote storage backends:

```json
{
    "remote-archive": {
        "remotes": {
            "team-s3": {
                "type": "s3",
                "credentials": "/path/to/s3-creds.json",
                "bucket": "crucible-archives",
                "tls-verify": false,
                "settings": {
                    "endpoint": "https://s3.example.com:9000",
                    "region": "us-east-1"
                }
            }
        },
        "default": "team-s3"
    }
}
```

### Key settings

- **httpd.port**: Web UI listening port
- **cdm-server.port**: CDM query API port
- **valkey.monitor.enabled**: Enable/disable the valkey
  traffic monitor
- **opensearch.instances**: Array of OpenSearch instances
  with name, host, and CDM version
- **opensearch.index-to**: Which instance receives new results
- **opensearch.query-from**: Which instances are available for
  queries
- **image-sourcing.use**: Enable/disable image sourcing
- **image-sourcing.services**: Per-architecture SIS
  configuration
- **remote-archive.remotes**: Named map of remote storage
  backends
- **remote-archive.default**: Default remote for `--remote
  default`

## OpenSearch instance management

OpenSearch supports multiple instances, enabling scenarios
like:

- A local instance running the latest CDM version for new
  results
- A remote shared instance running an older CDM version with
  historical data
- Separate instances for different teams or projects

### Index-to vs query-from

The **index-to** setting specifies the single instance where
new benchmark results are indexed. The **query-from** setting
is an array of instances that the CDM server queries when
searching for results. This allows querying across multiple
instances while only writing to one.

### Managing instances

The `crucible opensearch` command manages OpenSearch instance
configuration:

```bash
crucible opensearch list          # show configured instances
crucible opensearch add <name>    # add a new instance
crucible opensearch remove <name> # remove an instance
crucible opensearch set-index <n> # set the index-to instance
crucible opensearch repair        # repair OpenSearch state
```

## Image-sourcing service details

The image-sourcing service has unique characteristics compared
to other services because it supports per-architecture
instances.

### The "default" key

The `services.json` image-sourcing section uses a `"default"`
key that maps to the controller's native CPU architecture
(determined at runtime via `uname -m`). This avoids hardcoding
the architecture in the committed configuration file.

### Remote builders

Non-native architectures (e.g., aarch64 on an x86_64
controller) cannot be built locally due to buildah/QEMU
incompatibilities. These must point to remote builder hosts
with `"start": false`:

```json
"aarch64": {
    "start": false,
    "location": {
        "address": "arm-builder.example.com",
        "port": 8888,
        "protocol": "http"
    }
}
```

### Systemd service

For dedicated builder hosts, the image-sourcing service can
be installed as a systemd unit that starts automatically on
boot and restarts on failure:

```bash
bin/services/setup-image-sourcing-service.sh install
bin/services/setup-image-sourcing-service.sh uninstall
```

The systemd service integrates with `crucible update` — the
update process stops the service before updating and restarts
it afterward, using a flag file to survive the update script's
self-restart mechanism.

## Service interaction with crucible update

When `crucible update` runs, it needs to stop services to
avoid conflicts with the update process:

1. **Check for active runs** — refuse to update if a benchmark
   run is in progress
2. **Stop systemd image-sourcing** — if the systemd service is
   enabled and active, stop it and set a restart flag
3. **Stop all container services** — `service_control stop all`
4. **Perform the update** — git pull, subproject updates,
   controller image check
5. **Restart systemd image-sourcing** — if the restart flag
   exists, restart the service and remove the flag

The flag file (`/tmp/.crucible-restart-image-sourcing`)
persists across the update script's `exec` self-restart,
ensuring the service is properly restored after the update
completes.

## Logging

### Container logs

Service containers use the journald log driver. Access logs
via:

```bash
journalctl CONTAINER_NAME=crucible-opensearch
journalctl CONTAINER_NAME=crucible-httpd
journalctl CONTAINER_NAME=crucible-valkey
```

### Service-specific log files

Some services also write to dedicated log files:

- **OpenSearch**: `/var/lib/crucible/logs/opensearch.log`
- **Image-sourcing**: `/var/lib/crucible/logs/image-sourcing.log`

### Valkey monitor

For debugging roadblock synchronization issues, enable the
valkey monitor in `services.json`:

```json
{
    "valkey": {
        "monitor": {
            "enabled": true
        }
    }
}
```

This starts a companion container (`crucible-valkey-monitor`)
that logs all valkey commands in real time, useful for
diagnosing synchronization failures or message delivery
issues.

### Crucible log

The `crucible log` command provides access to the main
crucible run log, which includes service startup/shutdown
messages:

```bash
crucible log view last    # view the most recent run's log
```
