"""Small administrative operations invoked inside the controller container."""

import argparse
from pathlib import Path
import sys

from .jobs import JobStore
from .policy import PolicyError, rotate_token, validate_token_rotation_path


def _active_jobs(database: Path) -> int:
    if not database.is_file():
        return 0
    store = JobStore(database)
    try:
        for job in store.list_active():
            print(f"{job.mcp_job_id} {job.state.value}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    active = commands.add_parser("active-jobs")
    active.add_argument("--database", type=Path, required=True)
    validate = commands.add_parser("validate-token")
    validate.add_argument("--path", type=Path, required=True)
    rotate = commands.add_parser("rotate-token")
    rotate.add_argument("--path", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "active-jobs":
            return _active_jobs(args.database)
        if args.command == "validate-token":
            validate_token_rotation_path(args.path)
            return 0
        if args.command == "rotate-token":
            validate_token_rotation_path(args.path)
            rotate_token(args.path)
            return 0
    except (OSError, PolicyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
