import datetime
from pathlib import Path
import tempfile
import unittest

from egovorder.utils.download import parallel_download


class OfflineSource:
    """A pickleable source that records worker execution without using HTTP."""

    hostname = "offline.invalid"

    def __init__(self, path, fail=False):
        self.path = Path(path)
        self.fail = fail

    def sync(self, fromdate, todate, event):
        if self.fail:
            raise SystemExit(2)
        self.path.write_text(f"{fromdate.date()} {todate.date()}")

    def sync_daily(self, event):
        self.path.write_text("daily")

    def all_downloads(self, event):
        self.path.write_text("all")


class WorkerTests(unittest.TestCase):
    def test_date_range_downloads_work_in_separate_processes(self):
        date = datetime.datetime(2025, 6, 1)
        for aggregate in (True, False):
            with self.subTest(aggregate=aggregate), tempfile.TemporaryDirectory() as temp:
                paths = [Path(temp) / name for name in ("andhra", "telangana")]
                sources = [OfflineSource(path) for path in paths]
                self.assertTrue(parallel_download(sources, aggregate, date, date, None, False))
                self.assertEqual([path.read_text() for path in paths], ["2025-06-01 2025-06-01"] * 2)

    def test_daily_and_full_history_modes(self):
        for all_downloads, expected in ((False, "daily"), (True, "all")):
            with self.subTest(mode=expected), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "result"
                self.assertTrue(parallel_download(
                    [OfflineSource(path)], True, None, None, None, all_downloads,
                ))
                self.assertEqual(path.read_text(), expected)

    def test_worker_errors_reach_the_caller(self):
        with tempfile.TemporaryDirectory() as temp:
            date = datetime.datetime(2025, 6, 1)
            self.assertFalse(parallel_download(
                [OfflineSource(Path(temp) / "result", fail=True)], True, date, date, None, False,
            ))


if __name__ == "__main__":
    unittest.main()
