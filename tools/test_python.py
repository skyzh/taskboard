"""Run Taskboard's checkout tests by component."""

import argparse
import unittest

from checkout import ROOT, TEST_DIRS, add_packages, add_tests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest test names.")
    parser.add_argument("--require-no-skips", action="store_true", help="Require every selected test to run.")
    args = parser.parse_args()
    add_packages("client", "cli", "server")
    add_tests()
    loader = unittest.defaultTestLoader
    if args.tests:
        suite = loader.loadTestsFromNames(args.tests)
    else:
        suite = unittest.TestSuite(loader.discover(str(ROOT / directory)) for directory in TEST_DIRS)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return not (result.testsRun and result.wasSuccessful() and (not args.require_no_skips or not result.skipped))


if __name__ == "__main__":
    raise SystemExit(main())
