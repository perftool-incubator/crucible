import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


class TestTokenRotationLifecycle(unittest.TestCase):
    """Exercise the shell-level safety guarantees around token rotation."""

    repository = Path(__file__).resolve().parents[2]

    harness = r'''
set -u
unset CRUCIBLE_MCP_LIFECYCLE_LOCK_FD
source <(awk '
    /^function mcp_lifecycle_lock_acquire\(\)/ { capture=1 }
    /^function mcp_active_jobs\(\)/ { exit }
    capture { print }
' "$ROOT/bin/base")
source <(awk '
    /^function service_control\(\)/ { capture=1 }
    /^function git_get_status\(\)/ { exit }
    capture { print }
' "$ROOT/bin/base")

SERVICES_CFG="$ROOT/config/services.json"
stat() {
    if [ "${1:-}" = "-c" ] && [ "${2:-}" = "%u" ]; then
        printf '0\n'
    else
        command stat "$@"
    fi
}
jq_query() {
    case "${2:-}" in
        *token-file*) printf '%s\n' "$TEST_TOKEN" ;;
        *database*) printf '%s\n' "$TEST_DATABASE" ;;
        *) return 1 ;;
    esac
}
podman_running() { return "${PODMAN_RUNNING:-0}"; }
mcp_active_jobs() {
    if [ "${MODE}" = "service-control" ]; then
        if [ -n "${CRUCIBLE_MCP_LIFECYCLE_LOCK_FD:-}" ]; then
            printf 'locked\n' > "$LOCK_STATE"
        else
            printf 'unlocked\n' > "$LOCK_STATE"
        fi
    fi
    if [ -n "${ACTIVE_JOBS:-}" ]; then
        printf '%s\n' "$ACTIVE_JOBS"
    fi
    return "${ACTIVE_RC:-0}"
}
podman_ps() { :; }
podman_ps=podman_ps
stop_mcp_server_unlocked() {
    STOP_COUNT=$((STOP_COUNT + 1))
    if [ "${MODE}" = "service-control" ]; then
        printf '%s\n' "${CRUCIBLE_MCP_LIFECYCLE_LOCK_FD:-}" > "$STOP_LOCK_STATE"
    fi
    return "${STOP_RC:-0}"
}
start_mcp_server_unlocked() {
    START_COUNT=$((START_COUNT + 1))
    if [ -n "${START_MARKER:-}" ]; then
        : > "$START_MARKER"
    fi
    return "${START_RC:-0}"
}
python3() {
    if [ "${TEST_FAKE_ROTATION:-0}" = "1" ]; then
        ROTATION_CALLS=$((ROTATION_CALLS + 1))
        if [ "${PREFLIGHT_FAIL:-0}" = "1" ] && [ ${ROTATION_CALLS} -eq 1 ]; then
            return 1
        fi
        if [ "${ROTATION_FAIL:-0}" = "1" ] && [ ${ROTATION_CALLS} -gt 1 ]; then
            return 1
        fi
        if [ ${ROTATION_CALLS} -gt 1 ]; then
            printf 'new-token\n' > "$TEST_TOKEN"
        fi
        return 0
    fi
    command python3 "$@"
}
STOP_COUNT=0
START_COUNT=0
ROTATION_CALLS=0

case "${MODE}" in
    active-jobs)
        if rotate_mcp_token; then
            exit 11
        fi
        printf '%s %s %s\n' "$STOP_COUNT" "$START_COUNT" "$(cat "$TEST_TOKEN")" > "$REPORT"
        ;;
    restart)
        if ! rotate_mcp_token; then
            exit 12
        fi
        printf '%s %s %s\n' "$STOP_COUNT" "$START_COUNT" "$(cat "$TEST_TOKEN")" > "$REPORT"
        ;;
    restore)
        if rotate_mcp_token; then
            exit 13
        fi
        printf '%s %s\n' "$STOP_COUNT" "$START_COUNT" > "$REPORT"
        ;;
    preflight-failure)
        if rotate_mcp_token; then
            exit 15
        fi
        printf '%s %s\n' "$STOP_COUNT" "$START_COUNT" > "$REPORT"
        ;;
    service-control)
        service_control stop mcp-server
        printf '%s %s\n' "$(cat "$LOCK_STATE")" "$(cat "$STOP_LOCK_STATE")" > "$REPORT"
        ;;
    holder)
        mcp_lifecycle_lock_acquire
        : > "$READY"
        sleep 0.6
        mcp_lifecycle_lock_release
        ;;
    contender)
        start_mcp_server
        ;;
    *)
        exit 14
        ;;
esac
'''

    def run_harness(self, directory: Path, mode: str, **values) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        environment.update(
            {
                "ROOT": str(self.repository),
                "CRUCIBLE_HOME": str(self.repository),
                "MODE": mode,
                "TEST_DATABASE": str(directory / "jobs.db"),
                "TEST_TOKEN": str(directory / "token"),
                "REPORT": str(directory / "report"),
                "TEST_FAKE_ROTATION": "1",
                "LOCK_STATE": str(directory / "lock-state"),
                "STOP_LOCK_STATE": str(directory / "stop-lock-state"),
            }
        )
        environment.update({key: str(value) for key, value in values.items()})
        return subprocess.run(
            ["bash", "-c", self.harness],
            cwd=self.repository,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_active_jobs_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = directory / "token"
            token.write_text("old-token\n", encoding="utf-8")
            result = self.run_harness(
                directory,
                "active-jobs",
                PODMAN_RUNNING=1,
                ACTIVE_JOBS="job-1 running",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stop_count, start_count, token_contents = (directory / "report").read_text().split()
            self.assertEqual((stop_count, start_count), ("0", "0"))
            self.assertEqual(token_contents, "old-token")

    def test_running_service_is_restarted_after_successful_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = directory / "token"
            token.write_text("old-token\n", encoding="utf-8")
            result = self.run_harness(
                directory,
                "restart",
                PODMAN_RUNNING=0,
                ACTIVE_JOBS="",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stop_count, start_count, token_contents = (directory / "report").read_text().split()
            self.assertEqual((stop_count, start_count), ("1", "1"))
            self.assertNotEqual(token_contents, "old-token")

    def test_failed_rotation_restores_running_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            blocked_parent = directory / "not-a-directory"
            blocked_parent.write_text("blocking path", encoding="utf-8")
            result = self.run_harness(
                directory,
                "restore",
                TEST_TOKEN=str(blocked_parent / "token"),
                PODMAN_RUNNING=0,
                ACTIVE_JOBS="",
                ROTATION_FAIL=1,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stop_count, start_count = (directory / "report").read_text().split()
            self.assertEqual((stop_count, start_count), ("1", "1"))

    def test_invalid_token_preflight_does_not_stop_running_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "token").write_text("old-token\n", encoding="utf-8")
            result = self.run_harness(
                directory,
                "preflight-failure",
                PODMAN_RUNNING=0,
                ACTIVE_JOBS="",
                PREFLIGHT_FAIL=1,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stop_count, start_count = (directory / "report").read_text().split()
            self.assertEqual((stop_count, start_count), ("0", "0"))

    def test_service_start_waits_for_rotation_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = os.environ.copy()
            environment.update(
                {
                    "ROOT": str(self.repository),
                    "CRUCIBLE_HOME": str(self.repository),
                    "MODE": "holder",
                    "TEST_DATABASE": str(directory / "jobs.db"),
                    "TEST_TOKEN": str(directory / "token"),
                    "REPORT": str(directory / "report"),
                    "READY": str(directory / "ready"),
                }
            )
            holder = subprocess.Popen(
                ["bash", "-c", self.harness],
                cwd=self.repository,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                deadline = time.monotonic() + 2
                while not (directory / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((directory / "ready").exists(), "lock holder did not start")
                environment["MODE"] = "contender"
                started = time.monotonic()
                contender = subprocess.run(
                    ["bash", "-c", self.harness],
                    cwd=self.repository,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                elapsed = time.monotonic() - started
                self.assertEqual(contender.returncode, 0, contender.stderr)
                self.assertGreaterEqual(elapsed, 0.45)
            finally:
                holder.wait(timeout=2)

    def test_service_stop_checks_jobs_under_the_lifecycle_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = self.run_harness(directory, "service-control", ACTIVE_JOBS="")
            self.assertEqual(result.returncode, 0, result.stderr)
            job_lock_state, stop_lock_state = (directory / "report").read_text().split()
            self.assertEqual(job_lock_state, "locked")
            self.assertTrue(stop_lock_state.isdigit())


if __name__ == "__main__":
    unittest.main()
