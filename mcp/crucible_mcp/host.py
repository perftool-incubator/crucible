"""Helpers for running Crucible commands in the host execution context."""

from collections.abc import Mapping, Sequence


_CONTAINER_RUNTIME_ENVIRONMENT = (
    "CONTAINER_HOST",
    "CONTAINER_CONNECTION",
    "CONTAINERS_STORAGE_CONF",
    "CONTAINERS_CONF",
    "PODMAN_CONNECTIONS_CONF",
    "DOCKER_HOST",
    "SESSION_ID",
    "PYTHONPATH",
    "PYTHONHOME",
    "CRUCIBLE_MCP_HOST_SERVICE_STARTS",
)
_MCP_SESSION_ENVIRONMENT = (
    "CRUCIBLE_MCP_SESSION_ID",
    "CRUCIBLE_MCP_EVENT_FILE",
)


def host_context_command(
    command: Sequence[str], working_directory: str = "/"
) -> list[str]:
    """Enter the host namespaces/root before executing a command."""

    return [
        "nsenter",
        "--mount=/proc/1/ns/mnt",
        "--cgroup=/proc/1/ns/cgroup",
        "--net=/proc/1/ns/net",
        "--root=/proc/1/root",
        f"--wdns={working_directory}",
        "--",
        *command,
    ]


def host_context_environment(
    environment: Mapping[str, str], *, preserve_mcp_session: bool = False
) -> dict[str, str]:
    """Remove container-only runtime overrides before using host Podman."""

    result = dict(environment)
    for name in _CONTAINER_RUNTIME_ENVIRONMENT:
        result.pop(name, None)
    if not preserve_mcp_session:
        for name in _MCP_SESSION_ENVIRONMENT:
            result.pop(name, None)
    return result
