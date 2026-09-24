import unittest

from crucible_mcp.host import host_context_command, host_context_environment


class TestHostContext(unittest.TestCase):
    def test_command_enters_host_container_namespaces_and_requested_directory(self):
        command = host_context_command(
            ["/opt/crucible/bin/crucible", "run", "/var/lib/crucible/run-file.json"],
            "/opt/crucible",
        )

        self.assertEqual(
            command,
            [
                "nsenter",
                "--mount=/proc/1/ns/mnt",
                "--cgroup=/proc/1/ns/cgroup",
                "--net=/proc/1/ns/net",
                "--root=/proc/1/root",
                "--wdns=/opt/crucible",
                "--",
                "/opt/crucible/bin/crucible",
                "run",
                "/var/lib/crucible/run-file.json",
            ],
        )

    def test_environment_uses_host_podman_but_can_preserve_job_correlation(self):
        source = {
            "CONTAINER_HOST": "unix:///container/podman.sock",
            "CONTAINERS_STORAGE_CONF": "/container/storage.conf",
            "PYTHONPATH": "/opt/crucible/mcp",
            "SESSION_ID": "stale-session",
            "CRUCIBLE_MCP_SESSION_ID": "job-session",
            "CRUCIBLE_MCP_EVENT_FILE": "/var/lib/crucible/events.jsonl",
            "CRUCIBLE_MCP_HOST_SERVICE_STARTS": "true",
            "PATH": "/usr/bin:/bin",
        }

        result = host_context_environment(source, preserve_mcp_session=True)

        self.assertNotIn("CONTAINER_HOST", result)
        self.assertNotIn("CONTAINERS_STORAGE_CONF", result)
        self.assertNotIn("PYTHONPATH", result)
        self.assertNotIn("SESSION_ID", result)
        self.assertEqual(result["CRUCIBLE_MCP_SESSION_ID"], "job-session")
        self.assertEqual(
            result["CRUCIBLE_MCP_EVENT_FILE"], "/var/lib/crucible/events.jsonl"
        )
        self.assertNotIn("CRUCIBLE_MCP_HOST_SERVICE_STARTS", result)
        self.assertEqual(result["PATH"], "/usr/bin:/bin")


if __name__ == "__main__":
    unittest.main()
