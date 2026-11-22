"""Locate checkout and installed server resources."""

import os
from pathlib import Path


PACKAGE = Path(__file__).resolve().parent
repository = PACKAGE.parents[3] if len(PACKAGE.parents) > 3 else None
CHECKOUT = repository if repository and (repository / "tools" / "checkout.py").is_file() else None
DATA = (CHECKOUT / "state" if CHECKOUT else
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "taskboard")
DIST = CHECKOUT / "web" / "dist" if CHECKOUT else PACKAGE / "web"
