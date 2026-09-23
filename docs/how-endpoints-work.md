# How Endpoints Work

This document explains how crucible's endpoint system operates —
the abstraction layer that deploys benchmark and tool engines
across different infrastructure targets. Endpoints handle
validation, engine deployment, image management, service
discovery, and cleanup so that benchmarks and tools don't need
to know where they're running.

For the benchmark execution flow that uses endpoints, see
[how-benchmark-execution-works.md](how-benchmark-execution-works.md).
For tool collection across endpoints, see
[how-tool-collection-works.md](how-tool-collection-works.md).

## Overview

An endpoint represents a deployment target — a set of machines
or a cluster where benchmark engines will run. Crucible supports
three endpoint types:

- **remotehosts**: Deploys engines on remote Linux hosts via SSH,
  supporting two runtime modes: podman containers (default) or
  chroot environments (for lower overhead and direct hardware
  access)
- **kube**: Deploys engines as pods in a Kubernetes cluster
- **osp**: Deploys engines as VMs in an OpenStack environment

The endpoint abstraction means the same benchmark (e.g., uperf)
runs identically whether deployed as a podman container on a bare
metal host, a pod in Kubernetes, or a VM in OpenStack. The
endpoint handles all deployment-specific concerns.

A single run can use multiple endpoints — for example, running
clients on bare metal via remotehosts while running servers in
Kubernetes via kube.

## Endpoint lifecycle

Each endpoint goes through these phases during a run:

1. **Validation** — verify connectivity, detect capabilities
2. **Deployment** — create engines (containers, pods, or VMs)
3. **Execution** — engines run benchmarks and tools, synchronized
   via roadblock
4. **Cleanup** — tear down engines and associated resources

## Validation phase

Before deploying any engines, rickshaw validates each endpoint
to verify connectivity and discover its capabilities. The
endpoint runs in validate mode and reports structured output
that rickshaw parses.

> **Note**: Static schema validation of endpoint definition blocks is performed
> offline as part of `crucible validate` (deep run-file validation against
> `kube.json`, `remotehosts.json`, `kvm.json`, or `osp.json`). Live connectivity checks,
> SSH validation, and capability discovery described below occur only during `crucible run`.

### What validation reports

Endpoints output specific keywords that rickshaw-run.py parses:

- **`arch`** — CPU architectures available (e.g., `x86_64`,
  `aarch64`). Used to determine which container images to build.
- **`engine-userenv`** — maps each engine to its userenv.
  Format: `engine-userenv <role> <id> <userenv>`. One line per
  non-profiler engine.
- **`client`** / **`server`** — engine IDs for each role
- **`engine-types`** — what roles this endpoint supports
  (client, server, profiler, worker, master)

### Architecture detection

Each endpoint type detects architecture differently:

- **remotehosts**: Runs `uname -m` on each remote host via SSH.
  Reports native Linux architecture names directly.
- **kube**: Queries the Kubernetes API
  (`kubectl get nodes --output json`) and reads
  `node.status.nodeInfo.architecture`. Normalizes K8s names to
  Linux names: `amd64` → `x86_64`, `arm64` → `aarch64`.
- **osp**: Uses VM metadata.

Multi-architecture clusters (e.g., a K8s cluster with both
x86_64 and aarch64 nodes) report all detected architectures.
Rickshaw then sources separate container images for each
architecture.

## Engine deployment

### Remotehosts

The remotehosts endpoint deploys engines on remote Linux hosts
via SSH. It supports two runtime modes:

**Podman mode** (default, `"osruntime": "podman"`):
1. SSH to the remote host
2. Pull the container image (`podman pull`)
3. Start the engine container (`podman run`) with the bootstrap
   script mounted
4. The engine script runs inside the container

**Chroot mode** (`"osruntime": "chroot"`):
1. SSH to the remote host
2. Pull the container image (`podman pull`)
3. Create a container without starting it (`podman create`) to
   extract the filesystem
4. Mount the container filesystem
5. Execute the engine bootstrap inside a `chroot` of that
   filesystem

Chroot mode provides lower overhead and direct hardware access
compared to running inside a container. This is important for
latency-sensitive benchmarks (cyclictest, oslat) that need
direct access to CPUs, DPDK devices, or kernel tracing
infrastructure without container isolation layers.

**Host mounts are endpoint-specific.** For `remotehosts`, the
`host-mounts` setting binds explicit host paths into the engine
environment, for example an application socket or device file.
For `kube`, `host-mounts` instead enables or disables a small set
of built-in host-path mounts; it is not an arbitrary bind list.
Neither form is necessary simply because a benchmark reads
ordinary system, network, or process information. The runtime
already provides standard views of those interfaces, and the
effective view also depends on namespace and privilege options.

#### Effective Podman environment

The default `remotehosts` Podman engine is created with the
following settings:

| Setting | Effect |
|---------|--------|
| `--privileged` | Grants the container broad device and capability access and removes the normal container device restrictions. |
| `--pid=host` | Places the engine in the host PID namespace. Process information visible through `/proc` therefore describes the host, subject to the image and kernel's normal `/proc` behavior. |
| `--net=host` | Places the engine in the host network namespace. Host interfaces, routes, and network devices are visible without binding `/proc` or `/sys`. |
| `--ipc=host` (default) | Shares the host IPC namespace. This is replaced by `--shm-size` when that Podman setting is configured. |
| `--security-opt=label=disable` | Disables SELinux label separation for the engine. This changes labeling restrictions; it does not by itself add a filesystem mount. |

In addition to the standard container filesystem and namespace
views, Crucible explicitly bind-mounts these paths for the
Podman engine:

- the remote run data directory at `/shared-engines-dir`;
- `/lib/firmware`;
- `/lib/modules`;
- `/usr/src`; and
- `/var/run`.

The run file's `host-mounts` entries are appended to this list.
They are bind mounts from the remote endpoint host, not from the
Crucible controller. A `dest` is optional; when omitted, the
source path is used as the destination path. The optional
`podman-settings.device` entries are separate from host mounts:
they are passed as Podman's `--device` mappings and should be
used when a benchmark needs a particular device node.

Crucible does not explicitly bind-mount `/proc`, `/sys`, or
`/dev` in the `remotehosts` Podman command. Their visibility comes
from Podman's normal container setup together with the host PID
namespace, host network namespace, and privileged mode. Therefore,
the existence of a path inside the engine does not prove that it
is an explicit host bind mount, and adding a redundant `host-mounts`
entry can make a run less portable. Add an explicit mount or device
mapping only when the benchmark requires a particular host path or
device and the effective endpoint configuration does not already
provide it.

The `remotehosts` client and server engines use the same Podman
environment. Their role changes engine and tool configuration, but
does not change the default namespace or mount settings. Each
remote host is evaluated independently: a path available on one
remote is not assumed to exist on another.

The `remotehosts` `chroot` runtime has a different contract. It
bind-mounts the run data directory and recursively bind-mounts
`/proc`, `/dev`, `/sys`, `/lib/firmware`, `/lib/modules`, `/usr/src`,
`/boot`, and `/var/run` into the extracted image filesystem. In
chroot mode, these are actual host filesystem mounts, so the
Podman namespace guidance above does not apply.

#### Kubernetes host visibility

Kubernetes engines are configured separately from `remotehosts`.
For the host-like engine pods, Rickshaw requests host PID, host
network, and host IPC namespaces and marks the container
privileged. The kube endpoint's `host-mounts` object controls these
explicit host paths, all enabled by default:

| Setting | Host path | Scope |
|---------|-----------|-------|
| `firmware` | `/lib/firmware` | All pods |
| `run` | `/var/run` | Worker and master engine pods |
| `modules` | `/lib/modules` | Worker and master engine pods |

Kubernetes does not use the `remotehosts` list of arbitrary
`{src,dest}` host mounts. Additional volumes and volume mounts must
be supplied through the Kubernetes endpoint's container settings.
Whether a cluster permits privileged pods or host namespaces is a
cluster policy concern; a run can fail or have less visibility when
those permissions are restricted.

#### Inspecting the effective environment

When diagnosing a run, inspect the endpoint where the engine was
created. For a running `remotehosts` Podman engine, the following
commands show the mounts and the namespace links selected by
Podman:

```bash
ssh <remote> sudo podman inspect <container-name> \
    --format '{{json .Mounts}}'
ssh <remote> sudo podman inspect <container-name> \
    --format '{{json .HostConfig}}'
ssh <remote> sudo podman exec <container-name> findmnt
ssh <remote> sudo podman exec <container-name> ls -l /proc/1/ns /proc/self/ns
```

The endpoint process records the generated `podman create` command
under the message `Podman create command is`. Endpoint stdout and
stderr are redirected by `rickshaw-run` to the endpoint's run
directory; they are not sent through the normal Crucible logger.
The file is:

```text
<base-run-dir>/run/endpoint/<endpoint-label>/endpoint-stderrout.txt
```

After the endpoint exits, `rickshaw-run` compresses it to:

```text
<base-run-dir>/run/endpoint/<endpoint-label>/endpoint-stderrout.txt.xz
```

For example, inspect a completed endpoint log with:

```bash
xz -dc <base-run-dir>/run/endpoint/<endpoint-label>/endpoint-stderrout.txt.xz \
    | grep -A 20 "Podman create command"
```

While a run is in progress, read the uncompressed file directly.
The regular Crucible logger captures the controller and
`rickshaw-run` output, including the parent message that starts the
endpoint command, but it does not contain the endpoint's detailed
Python logging. The endpoint file is therefore the authoritative
source for the generated Podman command and the endpoint's remote
operation results.

The command log shows explicit mounts and device mappings, while
`podman inspect` and commands run inside the engine show the
effective configuration after Podman has created it. For
Kubernetes, inspect the generated pod with `kubectl get pod
<pod-name> -o yaml` and check `securityContext`, `hostPID`,
`hostNetwork`, `hostIPC`, `volumes`, and `volumeMounts`.

### Kubernetes

The kube endpoint deploys engines as pods:

1. **Namespace creation**: Creates a dedicated namespace for the
   run (with labels tracking ownership and run ID). If a
   namespace from a previous run exists, it's cleaned up first.
2. **Pod creation**: Each engine becomes a pod. Pod specs include
   the container image, resource requests, node selectors, and
   volume mounts.
3. **Architecture targeting**: If the cluster has multiple
   architectures, the endpoint adds a
   `kubernetes.io/arch` node selector to ensure pods land on
   nodes with the correct architecture.

### OpenStack (OSP)

The osp endpoint deploys engines as VM instances in an OpenStack
cluster. Each engine becomes a VM with the benchmark environment
installed.

### Engine persistence

Engines persist for the entire run — they are created once during
the deployment phase and execute all iterations and samples
sequentially. This avoids the overhead of creating and destroying
containers or pods for each sample.

## Image management

### The image map

Container images are passed to endpoints via a structured JSON
file (`image-map.json`) using the `--image-map=<filepath>` CLI
argument.  The file maps benchmark and tool names to their
container image URLs, organized by engine role, userenv, and
CPU architecture:

```json
{
    "uperf": {
        "all": {
            "rhubi9": {
                "x86_64": {
                    "image": "<registry>/<repo>:abc123_x86_64"
                }
            }
        }
    },
    "trafficgen": {
        "client": {
            "alma8": {
                "x86_64": {
                    "image": "<registry>/<repo>:def456_x86_64"
                }
            }
        }
    },
    "sysstat": {
        "all": {
            "fedora-latest": {
                "x86_64": {
                    "image": "<registry>/<repo>:ghi789_x86_64",
                    "auth-file": "/path/to/pull-token.json"
                }
            }
        }
    }
}
```

The role key is `all` for standard benchmarks or
`client`/`server` for benchmarks with split workshop files.
The optional `auth-file` field provides a path to a
Docker/Podman auth JSON file for private registries.

### Image pulling

Endpoints pull images in parallel across hosts or nodes.
For remotehosts, a thread pool handles concurrent pulls across
remote hosts. For kube, the Kubernetes runtime handles pulls
when pods are created.

### Private registry authentication

When an image requires authentication:

1. The auth file is included in the image string as a 6th field
2. The endpoint copies the auth file to the remote host or
   creates a Kubernetes image pull secret
3. The image pull uses the auth credentials
4. The auth file is cleaned up after pulling

### Image caching

Endpoints track which images have been pulled to avoid redundant
pulls. The remotehosts endpoint maintains an image census file
on each remote host that records which images are present and
when they were last used. Old images can be pruned based on a
configurable cache size.

## Client-server service discovery

Client-server benchmarks need clients to discover where servers
are running. The mechanism differs by endpoint type.

### Remotehosts

Clients connect directly to server IPs. Since both client and
server containers run on known hosts with known IPs, the server
publishes its host IP and ports via the roadblock messaging
system. The client reads the message and connects directly.

### Kubernetes

Kubernetes networking requires additional abstraction because
pod IPs are internal to the cluster:

1. During **server-start**, the server pod writes its pod IP
   and ports as a raw payload to `msgs/tx/svc`. The engine
   infrastructure wraps it in targeted messages to the
   endpoint and the paired client.
2. During **endpoint-start**, the kube endpoint creates a
   Kubernetes Service (ClusterIP, NodePort, or LoadBalancer)
   that routes to the server pod
3. The endpoint writes an **endpoint-start-end** message with
   the Service IP, replacing the raw pod IP
4. During **client-start**, the client reads from `msgs/rx/`,
   preferring `endpoint-start-end` messages (Service IP) over
   `server-start-end` messages (pod IP)

This allows the same benchmark client code to work on both
remotehosts (direct IP) and kube (Service IP) without
modification.

## Run file configuration

### Remotehosts example

```json
{
    "endpoints": [{
        "type": "remotehosts",
        "settings": {
            "user": "root"
        },
        "remotes": [{
            "engines": [
                { "role": "client", "ids": [1] },
                { "role": "server", "ids": [2] }
            ],
            "config": {
                "host": "testhost.example.com",
                "settings": {
                    "userenv": "rhubi9",
                    "osruntime": "podman",
                    "cpu-partitioning": false,
                    "host-mounts": [
                        { "src": "/srv/benchmark-data", "dest": "/mnt/benchmark-data" }
                    ]
                }
            }
        }]
    }]
}
```

Endpoint configurations in run files are validated against
endpoint-specific schemas (`rickshaw/schema/remotehosts.json`,
`rickshaw/schema/kube.json`, `rickshaw/schema/osp.json`).
The overall run file structure is validated against
`rickshaw/schema/run-file.json`.

Key settings:
- `osruntime`: `"podman"` (default) or `"chroot"`
- `cpu-partitioning`: Enable CPU isolation for latency-sensitive
  workloads
- `host-mounts`: Directories from the host to mount into the
  engine
- `userenv`: Base container image to use

### Kubernetes example

```json
{
    "endpoints": [{
        "type": "kube",
        "host": "k8s-controller.example.com",
        "user": "root",
        "engines": {
            "client": "1-2",
            "server": "3-4"
        },
        "host-mounts": {
            "run": true,
            "modules": true,
            "firmware": true
        }
    }]
}
```

### Per-engine configuration

The `kube` endpoint supports per-engine configuration through its `config`
array with `targets`. The `remotehosts` endpoint uses endpoint-level defaults
and per-remote overrides under `remotes[*].config.settings` instead.

## Cleanup

After a run completes (or fails), endpoints clean up their
resources:

### Remotehosts
- Stop and remove engine containers (or unmount chroot
  filesystems)
- Remove bootstrap scripts and temporary files from remote hosts
- Auth files cleaned up after image pulls

### Kubernetes

Before anything is deleted, diagnostics are collected and archived so
they survive the eventual namespace deletion. This happens both on
normal cleanup and (best-effort, without deleting anything) when a
run aborts early:

- Namespace-wide status (`kubectl get all --output wide`) and events
  (`kubectl get events --sort-by=.lastTimestamp`), written to the
  endpoint's sysinfo directory as `get-all.txt.xz` and `events.txt.xz`
- Per-pod `kubectl describe pod` output, written alongside each
  engine's container log as `<pod-name>.describe.txt.xz` next to
  `<engine>.txt.xz` in the run's engine-logs directory
- A `kubectl describe node` for each node that hosted an engine pod,
  written to the sysinfo directory as `node-<node-name>.describe.txt.xz`
- A `kubectl describe` of all Jobs, Services, and Secrets in the
  namespace (Secrets use `describe`, never `-o yaml`, so pull
  credentials are never written to disk), written to the sysinfo
  directory as `jobs.describe.txt.xz`, `services.describe.txt.xz`,
  and `secrets.describe.txt.xz`

Only then does cleanup proceed to delete all engine pods, Services
created for client-server communication, and image pull secrets, and
clean up namespace labels (or delete the namespace entirely).

### OSP
- Terminate VM instances
- Release floating IPs and network resources

## Comparison

| Aspect | Remotehosts | Kube | OSP |
|--------|-------------|------|-----|
| **Engine type** | Podman container or chroot | K8s pod | OpenStack VM |
| **Connectivity** | SSH | SSH + kubectl | SSH + OpenStack API |
| **Arch detection** | `uname -m` | K8s node API | VM metadata |
| **Service discovery** | Direct IP | K8s Service (ClusterIP/NodePort/LB) | Direct IP |
| **Image pull** | `podman pull` via SSH | K8s runtime | Hypervisor |
| **Auth support** | Docker auth JSON file | K8s ImagePullSecret | VM-specific |
| **CPU isolation** | cpu-partitioning setting | Pod resource limits | VM pinning |
| **Hardware access** | Full (especially chroot mode) | Limited by pod security | Full (VM) |
| **Overhead** | Low (chroot) to medium (podman) | Medium | High (full VM) |
| **Best for** | Bare metal testing, DPDK, RT | Cloud-native workloads | OpenStack validation |
