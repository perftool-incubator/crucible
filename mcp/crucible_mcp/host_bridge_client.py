"""Client for Crucible's private, allowlisted host-execution bridge."""

import argparse
import json
import os
import socket
import sys

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--working-directory", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
        print("ERROR: invalid host bridge command", file=sys.stderr)
        return 125

    request = {
        "command": command,
        "working_directory": args.working_directory,
        "environment": {
            name: os.environ[name]
            for name in ("CRUCIBLE_MCP_SESSION_ID", "CRUCIBLE_MCP_EVENT_FILE")
            if name in os.environ
        },
    }
    try:
        encoded = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("host bridge request is too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(args.socket)
            client.sendall(encoded)
            response = bytearray()
            while len(response) <= MAX_RESPONSE_BYTES:
                chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(response)))
                if not chunk:
                    break
                response.extend(chunk)
        if len(response) > MAX_RESPONSE_BYTES:
            raise ValueError("host bridge response is too large")
        payload = json.loads(response.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("returncode"), int):
            raise ValueError("invalid host bridge response")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: host operation bridge unavailable: {exc}", file=sys.stderr)
        return 125

    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    if payload.get("error"):
        print(f"ERROR: {payload['error']}", file=sys.stderr)
    return payload["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
