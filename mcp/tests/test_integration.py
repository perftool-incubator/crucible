"""Opt-in read-only checks against the local Crucible services.

Run with CRUCIBLE_MCP_INTEGRATION=1 after the target services are available.
The suite performs discovery and bounded reads only; it does not submit runs,
modify the logger database, or create/remove containers.
"""

import os
import unittest
from pathlib import Path

from crucible_mcp.operations import CrucibleOperations


INTEGRATION_ENABLED = os.environ.get("CRUCIBLE_MCP_INTEGRATION") == "1"


@unittest.skipUnless(INTEGRATION_ENABLED, "set CRUCIBLE_MCP_INTEGRATION=1 to run live checks")
class TestMCPIntegration(unittest.TestCase):
    def setUp(self):
        self.operations = CrucibleOperations(
            Path(os.environ.get("CRUCIBLE_HOME", "/opt/crucible")),
            cdm_base_url=os.environ.get("CRUCIBLE_MCP_CDM_URL", "http://127.0.0.1:3000"),
            log_db=Path(os.environ["CRUCIBLE_MCP_LOG_DB"])
            if os.environ.get("CRUCIBLE_MCP_LOG_DB")
            else None,
        )

    def test_cdm_run_discovery(self):
        result = self.operations.list_indexed_results(limit=1)
        self.assertLessEqual(len(result["run_ids"]), 1)
        self.assertEqual(result["count"], len(result["run_ids"]))

    def test_cdm_metric_query(self):
        required = ("CRUCIBLE_MCP_RUN", "CRUCIBLE_MCP_SOURCE", "CRUCIBLE_MCP_TYPE")
        if not all(os.environ.get(name) for name in required):
            self.skipTest("set CRUCIBLE_MCP_RUN, CRUCIBLE_MCP_SOURCE, and CRUCIBLE_MCP_TYPE")
        period = os.environ.get("CRUCIBLE_MCP_PERIOD")
        begin = int(os.environ["CRUCIBLE_MCP_BEGIN"]) if os.environ.get("CRUCIBLE_MCP_BEGIN") else None
        end = int(os.environ["CRUCIBLE_MCP_END"]) if os.environ.get("CRUCIBLE_MCP_END") else None
        if period is None and (begin is None or end is None):
            self.skipTest("set CRUCIBLE_MCP_PERIOD or both CRUCIBLE_MCP_BEGIN and CRUCIBLE_MCP_END")
        result = self.operations.get_indexed_metric(
            run=os.environ["CRUCIBLE_MCP_RUN"],
            source=os.environ["CRUCIBLE_MCP_SOURCE"],
            metric_type=os.environ["CRUCIBLE_MCP_TYPE"],
            period=period,
            begin=begin,
            end=end,
        )
        self.assertIsInstance(result, dict)

    def test_logger_database_queries(self):
        if not os.environ.get("CRUCIBLE_MCP_LOG_DB"):
            self.skipTest("set CRUCIBLE_MCP_LOG_DB to a logger database")
        info = self.operations.get_log_info()
        self.assertEqual(set(info), {"sessions", "lines", "sources"})
        sessions = self.operations.list_log_sessions(limit=1)
        self.assertLessEqual(len(sessions["sessions"]), 1)

if __name__ == "__main__":
    unittest.main()
