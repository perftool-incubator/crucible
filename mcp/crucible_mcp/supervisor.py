"""Run one MCP job inside a host-Podman-managed supervisor container.

The container is a sibling of the MCP service container.  Its main process
enters the host namespaces before invoking Crucible, while its own Podman
cgroup keeps the job alive if the MCP service is restarted.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .host import host_context_command, host_context_environment


_MAINTENANCE_OPERATIONS = frozenset(
    {
        "postprocess",
        "index",
        "archive_local_run",
        "unarchive_local_run",
        "delete_indexed_result",
    }
)


def _job_directory_path(path: Path) -> Path:
    """Validate the supervisor's fixed, mounted job-directory path."""

    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("supervisor paths must be normalized absolute paths")
    return path


def _write_json_atomically(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def run_job(
    operation: str,
    job_directory: Path,
    working_directory: Path,
    command: Sequence[str],
) -> int:
    """Execute a host-context Crucible command and persist its final outcome."""

    mounted_job_directory = _job_directory_path(job_directory)
    log_path = mounted_job_directory / "runner.log"
    outcome_path = mounted_job_directory / "supervisor-outcome.json"
    completion_path = mounted_job_directory / "processing-complete"

    try:
        with log_path.open("ab") as log:
            result = subprocess.run(
                host_context_command(command, str(working_directory)),
                cwd="/",
                env=host_context_environment(
                    os.environ, preserve_mcp_session=True
                ),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            exit_code = result.returncode
    except OSError as exc:
        with log_path.open("ab") as log:
            log.write(f"supervisor could not launch Crucible: {exc}\n".encode("utf-8"))
        exit_code = 127

    if exit_code == 0 and operation in _MAINTENANCE_OPERATIONS:
        temporary = completion_path.with_name(
            f".{completion_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(operation + "\n", encoding="utf-8")
        os.replace(temporary, completion_path)

    _write_json_atomically(
        outcome_path,
        {"operation": operation, "exit_code": exit_code},
    )
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one supervised Crucible MCP job")
    parser.add_argument("--operation", required=True)
    parser.add_argument("--job-directory", required=True, type=Path)
    parser.add_argument("--working-directory", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a Crucible command is required after --")
    try:
        exit_code = run_job(
            args.operation,
            args.job_directory,
            args.working_directory,
            command,
        )
    except (OSError, ValueError) as exc:
        print(f"MCP supervisor setup failed: {exc}", file=sys.stderr)
        exit_code = 127
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
