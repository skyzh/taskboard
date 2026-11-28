"""Run Taskboard's checkout tests by component."""

import argparse
import sys
import unittest

from checkout import ROOT, TEST_DIRS, add_packages, add_tests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest test names.")
    parser.add_argument("--require-no-skips", action="store_true", help="Require every selected test to run.")
    parser.add_argument("--durations", type=int, metavar="N", help="Report the N slowest tests on Python 3.12 or newer.")
    args = parser.parse_args()
    runner_options = {"verbosity": 1}
    if args.durations is not None:
        if args.durations < 1:
            parser.error("--durations requires a positive count")
        if sys.version_info < (3, 12):
            parser.error("--durations requires Python 3.12 or newer")
        runner_options["durations"] = args.durations
    add_packages("client", "cli", "server")
    add_tests()
    loader = unittest.defaultTestLoader
    if args.tests:
        suite = loader.loadTestsFromNames(args.tests)
    else:
        suite = unittest.TestSuite(loader.discover(str(ROOT / directory)) for directory in TEST_DIRS)
    result = unittest.TextTestRunner(**runner_options).run(suite)
    return not (result.testsRun and result.wasSuccessful() and (not args.require_no_skips or not result.skipped))


if __name__ == "__main__":
    raise SystemExit(main())
