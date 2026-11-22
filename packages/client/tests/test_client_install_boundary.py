import subprocess
import sys
import unittest

from tools.checkout import ROOT


class ClientInstallBoundaryTest(unittest.TestCase):
    def test_connection_import_has_no_cli_or_server_dependency(self):
        subprocess.run(
            [sys.executable, "-I", "-B", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
from taskboard_client import SharedBoard
from taskboard_client.references import identifier
assert SharedBoard("fixture").service == "fixture"
assert identifier("course-4") == ("COURSE", 4)
assert not {"taskboard_cli", "tasks_core", "tasks_service", "taskboard_mcp"} & sys.modules.keys()
""", str(ROOT / "packages/client/src")],
            check=True, timeout=10,
        )
