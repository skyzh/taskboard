# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.0.0"]
# ///
"""Launch the checkout's local Taskboard MCP server."""

from pathlib import Path
import sys

root = Path(__file__).resolve().parent
sys.path[:0] = [str(root / "packages" / component / "src") for component in ("client", "mcp")]

from taskboard_mcp import main

if __name__ == "__main__":
    main()
