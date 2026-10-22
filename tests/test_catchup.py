from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from egovorder import catchup as job
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

    def test_default_starts_and_210_minute_budget(self):
        for source, expected in (("andhra_goir", None), ("telangana_goir", date(2014, 6, 2)),
                                 ("uttarakhand_gos", None)):
            with self.subTest(source=source):
                env = dict(self.env, SOURCE=source, FROM_DATE="", RUN_TIME_MINS="")
                settings = Settings.from_env(env)
                self.assertEqual(settings.start, expected)
                self.assertEqual(settings.minutes, 210)

    def test_catchup_seeds_once_from_ia_then_uses_saved_progress(self):
        for source, prefix in (("andhra_goir", "in.gov.andhra.goir."),
                               ("uttarakhand_gos", "in.gov.uttarakhand.go.")):
            with self.subTest(source=source):
                self.env.update(SOURCE=source, FROM_DATE='', TO_DATE='02-06-2025',
                                FAKE_IA_LATEST_DATE='2025-06-01T00:00:00Z',
                                DATA_DIR=str(self.data / source))
                self.log.write_text('')
                self.run_catchup()
                self.assertEqual(len(self.calls("sync.py")), 2)
                self.assertEqual(len(self.calls("iasync.py")), 2)
                settings = Settings.from_env(self.env)
                self.assertEqual(settings.next_date(), date(2025, 6, 3))
                self.assertTrue(settings.checkpoint.name.endswith('-latest.json'))
                for call in self.calls("sync.py"):
                    self.assertIn(prefix, call["done"])
                seed_queries = [call for call in self.calls('ia') if '-i' not in call['args']]
                self.assertEqual(len(seed_queries), 1)
                self.log.write_text('')
                self.env.update(TO_DATE='03-06-2025', FAKE_IA_LATEST_DATE='2026-10-01T00:00:00Z')
                self.run_catchup()
                self.assertEqual(settings.next_date(), date(2025, 6, 4))
                self.assertEqual(len(self.calls('sync.py')), 1)
                self.assertEqual([call for call in self.calls('ia') if '-i' not in call['args']], [])

    def test_empty_archive_cannot_start_an_unrequested_backfill(self):
        for source in ('andhra_goir', 'uttarakhand_gos'):
            with self.subTest(source=source):
                self.env.update(SOURCE=source, FROM_DATE='', FAKE_IA_LATEST_DATE='')
                self.run_catchup(success=False)
                self.assertEqual(self.calls('sync.py'), [])
                self.assertFalse(Settings.from_env(self.env).checkpoint.exists())

    def legacy_andhra_state(self):
        root = self.data / 'backfill' / 'latest' / 'production'
        root.mkdir(parents=True)
        (root / 'latest.json').write_text(json.dumps({
            'start_date': '2025-06-01', 'next_date': '2025-06-02', 'through': '2025-06-02',
        }))
        for folder, suffix in (('raw', '.pdf'), ('metatags', '.xml')):
            pending = root / 'data' / folder / 'andhra_goir' / '2025-06-02' / ('E-123' + suffix)
            pending.parent.mkdir(parents=True)
            pending.write_bytes(b'pending legacy upload')
        return root

    def test_andhra_migration_preserves_pending_uploads_and_progress_across_retries(self):
        legacy = self.legacy_andhra_state()
        self.env.update(FROM_DATE='', FAIL_UPLOAD='1')
        settings = Settings.from_env(self.env)
        self.run_catchup(success=False)
        self.assertEqual([call['command'] for call in self.calls()], ['iasync.py'])
        self.assertEqual(settings.next_date(), date(2025, 6, 2))
        self.assertEqual(len(list(settings.pending_files())), 2)
        for pending in settings.pending_files():
            self.assertEqual(pending.read_bytes(), b'pending legacy upload')
            original = legacy / 'data' / pending.relative_to(self.data)
            self.assertEqual(original.read_bytes(), pending.read_bytes())

        self.env.pop('FAIL_UPLOAD')
        self.log.write_text('')
        self.run_catchup()
        self.assertEqual(self.calls()[0]['command'], 'iasync.py')
        self.assertEqual(len(self.calls('sync.py')), 1)
        args = self.calls('sync.py')[0]['args']
        self.assertEqual(args[args.index('-t') + 1], '02-06-2025')
        self.assertEqual(settings.next_date(), date(2025, 6, 3))
        self.assertEqual(list(settings.pending_files()), [])
        self.assertEqual([call for call in self.calls('ia') if '-i' not in call['args']], [])

        # The old cache must not reimport files or rewind completed progress.
        self.log.write_text('')
        self.run_catchup()
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(settings.pending_files()), [])

    def test_andhra_migration_only_applies_to_production_latest_catchup(self):
        self.legacy_andhra_state()
        for values in ({'FROM_DATE': '01-06-2025'},
                       {'FROM_DATE': '', 'TESTING_MODE': 'true'},
                       {'FROM_DATE': '', 'SOURCE': 'uttarakhand_gos'}):
            with self.subTest(values=values):
                settings = Settings.from_env(dict(self.env, **values))
                job.migrate_andhra_catchup(settings)
                self.assertFalse(settings.checkpoint.exists())
                self.assertEqual(list(settings.pending_files()), [])

    def test_andhra_migration_rejects_an_invalid_checkpoint_before_uploading(self):
        legacy = self.legacy_andhra_state()
        (legacy / 'latest.json').write_text(json.dumps({
            'start_date': '2025-06-02', 'next_date': '2025-06-01',
        }))
        self.env['FROM_DATE'] = ''
        self.run_catchup(success=False)
        settings = Settings.from_env(self.env)
        self.assertFalse(settings.checkpoint.exists())
        self.assertEqual(list(settings.pending_files()), [])
        self.assertEqual(self.calls(), [])

    def test_andhra_migration_does_not_overwrite_conflicting_pending_files(self):
        self.legacy_andhra_state()
        pending = self.data / 'raw' / 'andhra_goir' / '2025-06-02' / 'E-123.pdf'
        pending.parent.mkdir(parents=True)
        pending.write_bytes(b'existing pending upload')
        self.env['FROM_DATE'] = ''
        self.run_catchup(success=False)
        self.assertEqual(pending.read_bytes(), b'existing pending upload')
        self.assertFalse(Settings.from_env(self.env).checkpoint.exists())
        self.assertEqual(self.calls(), [])

    def test_uttarakhand_failed_day_is_retained_despite_newer_archive_items(self):
        self.env.update(SOURCE='uttarakhand_gos', FROM_DATE='', FAIL_DATE='01-06-2025')
        self.run_catchup(success=False)
        settings = Settings.from_env(self.env)
        self.assertEqual(settings.next_date(), date(2025, 6, 1))
        self.env.pop('FAIL_DATE')
        self.env['FAKE_IA_LATEST_DATE'] = '2026-10-01T00:00:00Z'
        self.log.write_text('')
        self.run_catchup()
        self.assertEqual(self.calls()[0]['command'], 'iasync.py')
        args = self.calls('sync.py')[0]['args']
        self.assertEqual(args[args.index('-t') + 1], '01-06-2025')
        self.assertEqual(settings.next_date(), date(2025, 6, 3))

    def test_uttarakhand_default_start_validation_needs_no_archive_query(self):
        self.env.update(SOURCE='uttarakhand_gos', FROM_DATE='')
        self.run_catchup('--validate')
        self.assertEqual(self.calls(), [])

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

    def test_recent_archive_pauses_with_pending_uploads_before_the_step_timeout(self):
        pending = self.data / "raw" / "andhra_goir" / "unfinished.pdf"
        self.env["RUN_TIME_MINS"] = "30"

        def expire(args, env, deadline):
            self.assertEqual(args, ["bash", "scripts/archive.sh"])
            self.assertEqual(env["RUN_TIME_MINS"], "30")
            self.assertEqual(deadline, 100 + 210 * 60)
            pending.parent.mkdir(parents=True)
            pending.write_bytes(b"pending upload")
            raise BudgetExpired

        with patch.dict(os.environ, self.env), patch.object(job, "run_command", side_effect=expire), \
             patch.object(job.time, "monotonic", return_value=100):
            self.assertEqual(job.main(["--recent"]), 0)
        self.assertEqual(pending.read_bytes(), b"pending upload")
        self.assertFalse(Settings.from_env(self.env).checkpoint.exists())

    def test_recent_archive_error_is_not_reported_as_a_budget_pause(self):
        with patch.dict(os.environ, self.env), \
             patch.object(job, "run_command", side_effect=subprocess.CalledProcessError(7, ["bash", "scripts/archive.sh"])):
            self.assertEqual(job.main(["--recent"]), 1)

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
