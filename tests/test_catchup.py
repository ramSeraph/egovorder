from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from egovorder.catchup import BudgetExpired, Settings, catch_up, run_command
from test_archive_script import COMMAND_STUB, IA_STUB


ROOT = Path(__file__).resolve().parents[1]


class CatchupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.log = self.root / "commands.jsonl"
        self.env = dict(
            os.environ,
            PATH=f"{self.root}:{os.environ['PATH']}",
            SOURCE="andhra_goir",
            FROM_DATE="01-06-2025",
            TO_DATE="02-06-2025",
            RUN_TIME_MINS="1",
            TESTING_MODE="false",
            IA_ACCESS_KEY="fake-access",
            IA_SECRET_KEY="fake-secret",
            PROXY_IP_ADDRESS="proxy.invalid",
            PROXY_AUTH="fake-user:fake-password",
            DATA_DIR=str(self.data),
            RUN_LOG=str(self.log),
        )
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "TESTING"):
            self.env.pop(name, None)
        stub = COMMAND_STUB.replace(
            'sys.exit(int(os.environ.get("FAIL_SYNC", "0")))',
            'sys.exit(1 if args[args.index("-t") + 1] == os.environ.get("FAIL_DATE") else 0)',
        )
        for name, code in (("python", stub), ("ia", IA_STUB)):
            executable = self.root / name
            executable.write_text(f"#!{sys.executable}\n" + code)
            executable.chmod(0o755)

    def run_catchup(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, "-m", "egovorder.catchup", *args],
            cwd=ROOT, env=self.env, text=True, capture_output=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        for name in ("IA_ACCESS_KEY", "IA_SECRET_KEY", "PROXY_AUTH"):
            if self.env.get(name):
                self.assertNotIn(self.env[name], result.stdout + result.stderr)
        return result

    def calls(self, command=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if command is None or call["command"] == command]

    def test_uploads_each_day_and_resumes_with_a_later_end_date(self):
        self.run_catchup()
        calls = [call for call in self.calls() if call["command"] in ("sync.py", "iasync.py")]
        self.assertEqual([call["command"] for call in calls], ["sync.py", "iasync.py"] * 2)
        self.assertEqual(Settings.from_env(self.env).next_date(), date(2025, 6, 3))
        self.log.write_text("")
        self.env["TO_DATE"] = "03-06-2025"
        self.run_catchup()
        download = self.calls("sync.py")[0]
        self.assertEqual(len(self.calls("sync.py")), 1)
        self.assertEqual(download["args"][download["args"].index("-t") + 1], "03-06-2025")
        self.assertEqual(download["args"][download["args"].index("-T") + 1], "03-06-2025")
        self.assertIn("-L", download["args"])

    def test_failed_day_is_retried_and_leftovers_upload_first(self):
        self.env["FAIL_DATE"] = "02-06-2025"
        self.run_catchup(success=False)
        settings = Settings.from_env(self.env)
        self.assertEqual(settings.next_date(), date(2025, 6, 2))
        self.assertTrue(list(settings.pending_files()))
        self.env.pop("FAIL_DATE")
        self.log.write_text("")
        self.run_catchup()
        self.assertEqual(self.calls()[0]["command"], "iasync.py")
        self.assertEqual(len(self.calls("sync.py")), 1)
        self.assertEqual(settings.next_date(), date(2025, 6, 3))
        self.assertEqual(list(settings.pending_files()), [])

    def test_upload_failure_preserves_files_and_does_not_advance(self):
        self.env["FAIL_UPLOAD"] = "1"
        self.run_catchup(success=False)
        settings = Settings.from_env(self.env)
        self.assertFalse(settings.checkpoint.exists())
        self.assertTrue(list(settings.pending_files()))
        self.log.write_text("")
        self.run_catchup(success=False)
        self.assertEqual([call["command"] for call in self.calls()], ["iasync.py"])
        self.assertTrue(list(settings.pending_files()))

    def test_sandbox_checkpoints_and_uploads_are_separate(self):
        self.env["TESTING_MODE"] = "true"
        settings = Settings.from_env(self.env)
        self.run_catchup()
        self.assertIn("-sandbox-", settings.checkpoint.name)
        for upload in self.calls("iasync.py"):
            self.assertIn("-S", upload["args"])
            self.assertIsNone(upload["proxy"])
        self.env["TESTING_MODE"] = "false"
        self.assertEqual(Settings.from_env(self.env).next_date(), date(2025, 6, 1))

    def test_changed_start_date_gets_its_own_progress(self):
        self.run_catchup()
        self.env["FROM_DATE"] = "02-06-2025"
        self.assertEqual(Settings.from_env(self.env).next_date(), date(2025, 6, 2))

    def test_defaults_use_historical_start_and_210_minutes(self):
        for source, expected in (("andhra_goir", date(2008, 6, 1)), ("telangana_goir", date(2014, 6, 2))):
            with self.subTest(source=source):
                env = dict(self.env, SOURCE=source, FROM_DATE="", RUN_TIME_MINS="")
                settings = Settings.from_env(env)
                self.assertEqual(settings.start, expected)
                self.assertEqual(settings.minutes, 210)

    def test_budget_expiry_does_not_advance_an_unfinished_day(self):
        settings = Settings.from_env(self.env)
        pending = self.data / "raw" / settings.source / "unfinished.pdf"
        deadlines = []

        def expire(args, env, deadline):
            deadlines.append(deadline)
            self.assertEqual(env["EGOVORDER_STRICT_DOWNLOADS"], "1")
            pending.parent.mkdir(parents=True)
            pending.write_bytes(b"unfinished")
            raise BudgetExpired

        with self.assertRaises(BudgetExpired):
            catch_up(settings, runner=expire, clock=lambda: 100)
        self.assertEqual(deadlines, [160])
        self.assertFalse(settings.checkpoint.exists())
        self.assertTrue(pending.exists())

    def test_total_budget_is_shared_by_all_days(self):
        settings = Settings.from_env(self.env)
        now = [100]
        calls = []

        def finish_day(args, env, deadline):
            calls.append(env["FROM_DATE"])
            self.assertEqual(deadline, 160)
            now[0] += 61

        with self.assertRaises(BudgetExpired):
            catch_up(settings, runner=finish_day, clock=lambda: now[0])
        self.assertEqual(calls, ["01-06-2025"])
        self.assertEqual(settings.next_date(), date(2025, 6, 2))

    def test_validate_requires_no_proxy_or_network(self):
        self.env.pop("PROXY_IP_ADDRESS")
        self.run_catchup("--validate")
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.data.exists())

    def test_invalid_settings_fail_before_services(self):
        original = self.env.copy()
        for values in (
            {"SOURCE": "central"}, {"RUN_TIME_MINS": "0"}, {"RUN_TIME_MINS": "211"},
            {"RUN_TIME_MINS": "invalid"}, {"FROM_DATE": "31-02-2025"},
            {"FROM_DATE": "03-06-2025"}, {"TESTING_MODE": "invalid"},
            {"IA_ACCESS_KEY": ""}, {"IA_SECRET_KEY": ""},
        ):
            with self.subTest(values=values):
                self.env = dict(original, **values)
                self.run_catchup("--validate", success=False)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.data.exists())

    def test_invalid_checkpoint_stops_without_service_calls(self):
        settings = Settings.from_env(self.env)
        settings.checkpoint.parent.mkdir(parents=True)
        settings.checkpoint.write_text('{"next_date": "bad"}')
        self.run_catchup(success=False)
        self.assertEqual(self.calls(), [])

    def test_timeout_stops_descendants_that_ignore_termination(self):
        marker = self.root / "child-survived"
        child_code = (
            "import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"time.sleep(0.6); Path({str(marker)!r}).write_text('unexpected')"
        )
        parent_code = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "time.sleep(30)"
        )
        with self.assertRaises(BudgetExpired):
            run_command(
                [sys.executable, "-c", parent_code], self.env, time.monotonic() + 0.3
            )
        time.sleep(0.5)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
