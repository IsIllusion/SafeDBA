"""Machine-readable integration results; skipped tests are not a green run."""

import argparse
import json
from pathlib import Path
import sys
import time
import unittest


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def record(self, test, status):
        self.records.append({"test": test.id(), "status": status, "elapsed_seconds": round(time.monotonic() - getattr(self, "started", time.monotonic()), 4)})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, "failed" if issubclass(err[0], test.failureException) else "error")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, "unexpected_success")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    valid = result.wasSuccessful() and result.testsRun > 0 and not result.skipped and not result.expectedFailures
    report = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "passed": valid, "cases": result.records, "live_llm_used": False}
    Path(args.report).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
