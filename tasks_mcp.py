# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.0.0"]
#
# [tool.uv]
# exclude-newer = "7 days"
# ///
"""Launch taskboard_mcp from this checkout."""

from tools.checkout import add_packages

add_packages("client", "mcp")

from taskboard_mcp import main

if __name__ == "__main__":
    raise SystemExit(main())
