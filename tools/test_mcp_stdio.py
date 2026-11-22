"""Run the two-agent stdio integration fixture."""

from pathlib import Path
from runpy import run_path


if __name__ == "__main__":
    run_path(str(Path(__file__).resolve().parents[1] / "tests/integration/mcp_stdio.py"), run_name="__main__")
