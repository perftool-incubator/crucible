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

### What validation reports

Endpoints output specific keywords that rickshaw-run.py parses:

- **`arch`** — CPU architectures available (e.g., `x86_64`,
  `aarch64`). Used to determine which container images to build.
- **`userenv`** — user environments (base images) available.
  Repeated for each userenv.
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

**Host mounts**: The `host-mounts` setting allows mounting host
directories into the engine's environment. This is commonly
used to expose device files, `/proc`, `/sys`, or application
sockets to the engine.

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

### The image format

Container images are passed to endpoints using a structured
format:

```
benchmark::role::userenv::arch::image_url[::auth_file]
```

Examples:
```
uperf::all::rhubi9::x86_64::quay.io/crucible/engines:abc123_x86_64
trafficgen::client::alma8::x86_64::quay.io/crucible/engines:def456_x86_64
sysstat::all::fedora-latest::x86_64::quay.io/crucible/engines:ghi789_x86_64
```

The `role` field is `all` for standard benchmarks or
`client`/`server` for benchmarks with split workshop files.

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

1. During **server-start**, the server pod publishes its pod IP
   and ports via `msgs/tx/svc`
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
                    "host-mounts": []
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
        "settings": {
            "user": "root"
        },
        "remotes": [{
            "engines": [
                { "role": "client", "ids": [1, 2] },
                { "role": "server", "ids": [3, 4] }
            ],
            "config": {
                "host": "k8s-controller.example.com",
                "settings": {
                    "userenv": "rhubi9",
                    "controller-ip-address": "10.0.0.1"
                }
            }
        }]
    }]
}
```

### Per-engine configuration

Both endpoint types support per-engine configuration through a
`config` array with `targets`. This allows different settings
for different engines — for example, different userenvs for
client vs server, or node selection for specific engine IDs.

## Cleanup

After a run completes (or fails), endpoints clean up their
resources:

### Remotehosts
- Stop and remove engine containers (or unmount chroot
  filesystems)
- Remove bootstrap scripts and temporary files from remote hosts
- Auth files cleaned up after image pulls

### Kubernetes
- Delete all engine pods
- Delete Services created for client-server communication
- Delete image pull secrets
- Clean up namespace labels (or delete the namespace entirely)

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
