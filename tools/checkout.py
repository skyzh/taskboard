"""Load the source distributions for commands run from a checkout."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def add_packages(*components):
    sys.path[:0] = [str(ROOT / "packages" / name / "src") for name in components]

TEST_DIRS = (
    "packages/client/tests",
    "packages/cli/tests",
    "packages/server/tests",
    "tests/integration",
)


def add_tests():
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))
    sys.path[:0] = [str(ROOT / directory) for directory in TEST_DIRS]
