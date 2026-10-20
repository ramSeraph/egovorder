import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

COMMAND_STUB = r"""
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
source = os.environ["SOURCE"]
if args and args[0] in ("sync.py", "iasync.py"):
    command = args[0]
    entry = {
        "command": command, "args": args,
        "proxy": os.environ.get("https_proxy"),
        "testing": os.environ.get("TESTING"),
    }
    if "-L" in args:
        entry["done"] = Path(args[args.index("-L") + 1]).read_text()
    with open(os.environ["RUN_LOG"], "a") as log:
        log.write(json.dumps(entry) + "\n")
    if command == "sync.py":
        for folder, suffix in (("raw", ".pdf"), ("metatags", ".xml")):
            path = Path(os.environ["DATA_DIR"]) / folder / source / "2025-06-01" / ("order" + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("offline fixture")
        sys.exit(int(os.environ.get("FAIL_SYNC", "0")))
    sys.exit(int(os.environ.get("FAIL_UPLOAD", "0")))
os.execv(sys.executable, [sys.executable, *args])
"""

IA_STUB = r"""
import json
import os
import sys

with open(os.environ["RUN_LOG"], "a") as log:
    log.write(json.dumps({
        "command": "ia", "args": sys.argv[1:], "proxy": os.environ.get("https_proxy")
    }) + "\n")
if "-i" in sys.argv:
    if "NOT date:*" not in sys.argv[2]:
        prefix = "egaztest." if os.environ.get("TESTING") == "1" else ""
        state = os.environ["SOURCE"].removesuffix("_goir")
        print(f"{prefix}in.gov.{state}.goir.2025-06-01.known-order")
else:
    print('{"date": "2025-06-01T00:00:00Z"}')
"""


class ArchiveScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data = root / "data"
        self.log = root / "commands.jsonl"
        self.env = dict(
            os.environ,
            PATH=f"{root}:{os.environ['PATH']}",
            SOURCE="andhra_goir",
            TO_DATE="30-06-2025",
            IA_ACCESS_KEY="fake-access",
            IA_SECRET_KEY="fake-secret",
            PROXY_IP_ADDRESS="proxy.invalid",
            PROXY_AUTH="fake-user:fake-password",
            DATA_DIR=str(self.data),
            RUN_LOG=str(self.log),
        )
        for name in (
            "FROM_DATE", "LAST_N_DAYS", "RUN_TIME_MINS", "TESTING_MODE", "TESTING",
            "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        ):
            self.env.pop(name, None)
        for name, code in (("python", COMMAND_STUB), ("ia", IA_STUB)):
            executable = root / name
            executable.write_text(f"#!{sys.executable}\n" + code)
            executable.chmod(0o755)

    def run_archive(self, *args, success=True):
        result = subprocess.run(
            ["bash", "scripts/archive.sh", *args],
            cwd=ROOT, env=self.env, text=True, capture_output=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(self.env.get("PROXY_AUTH", ""), result.stdout + result.stderr)
        return result

    def calls(self, command=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if command is None or call["command"] == command]

    def test_defaults_match_existing_scheduled_runs(self):
        for source in ("andhra_goir", "telangana_goir"):
            with self.subTest(source=source):
                self.env["SOURCE"] = source
                self.run_archive()
                download = self.calls("sync.py")[-1]
                args = download["args"]
                self.assertEqual(args[args.index("-s") + 1], source)
                self.assertEqual(args[args.index("-t") + 1], "01-01-2025")
                self.assertEqual(args[args.index("-T") + 1], "30-06-2025")
                self.assertEqual(args[args.index("-W") + 1], "1800")
                self.assertIn(f"in.gov.{source.removesuffix('_goir')}.goir.", download["done"])
                self.assertEqual(self.calls()[-1]["command"], "iasync.py")

    def test_proxy_is_used_only_for_portal_downloads(self):
        self.run_archive()
        for call in self.calls():
            with self.subTest(command=call["command"]):
                expected = "http://fake-user:fake-password@proxy.invalid:8080" if call["command"] == "sync.py" else None
                self.assertEqual(call["proxy"], expected)

    def test_explicit_dates_override_default_lookback(self):
        self.env.update(FROM_DATE="01-06-2025", RUN_TIME_MINS="005")
        self.run_archive()
        args = self.calls("sync.py")[0]["args"]
        self.assertEqual(args[args.index("-t") + 1], "01-06-2025")
        self.assertEqual(args[args.index("-W") + 1], "300")

    def test_empty_lookback_resumes_from_archive(self):
        self.env["LAST_N_DAYS"] = ""
        self.run_archive()
        args = self.calls("sync.py")[0]["args"]
        self.assertEqual(args[args.index("-t") + 1], "01-06-2025")
        self.assertNotIn("-L", args)

    def test_sandbox_queries_and_uploads_stay_in_sandbox(self):
        self.env["TESTING_MODE"] = "true"
        self.run_archive()
        self.assertIn("identifier:egaztest.in.gov.andhra.goir.", self.calls("ia")[0]["args"][1])
        self.assertEqual(self.calls("sync.py")[0]["done"], "in.gov.andhra.goir.2025-06-01.known-order\n")
        self.assertIn("-S", self.calls("iasync.py")[0]["args"])

    def test_success_only_cleans_files_for_selected_source(self):
        other = self.data / "raw" / "telangana_goir" / "unfinished.pdf"
        other.parent.mkdir(parents=True)
        other.write_text("keep")
        self.run_archive()
        self.assertTrue(other.exists())
        self.assertEqual(list((self.data / "raw" / "andhra_goir").rglob("*.pdf")), [])
        self.assertEqual(list((self.data / "metatags" / "andhra_goir").rglob("*.xml")), [])

    def test_upload_failure_keeps_downloads_for_next_run(self):
        self.env["FAIL_UPLOAD"] = "1"
        self.run_archive(success=False)
        self.assertTrue(list((self.data / "raw" / "andhra_goir").rglob("*.pdf")))
        self.assertTrue(list((self.data / "metatags" / "andhra_goir").rglob("*.xml")))

    def test_download_failure_stops_uploads_and_keeps_partial_files(self):
        self.env["FAIL_SYNC"] = "1"
        self.run_archive(success=False)
        self.assertEqual(self.calls("iasync.py"), [])
        self.assertTrue(list((self.data / "raw" / "andhra_goir").rglob("*.pdf")))

    def test_validation_does_not_access_services_or_require_proxy_host(self):
        self.env.pop("PROXY_IP_ADDRESS")
        self.env["SOURCE"] = "telangana_goir"
        self.run_archive("--validate")
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.data.exists())

    def test_bad_inputs_fail_before_any_service_call(self):
        cases = (
            {"SOURCE": "central"},
            {"RUN_TIME_MINS": "0"},
            {"RUN_TIME_MINS": "211"},
            {"RUN_TIME_MINS": "301"},
            {"LAST_N_DAYS": "-1"},
            {"TO_DATE": "31-02-2025"},
            {"FROM_DATE": "01-07-2025"},
            {"TESTING_MODE": "bad"},
            {"IA_ACCESS_KEY": ""},
            {"PROXY_IP_ADDRESS": ""},
        )
        original = self.env.copy()
        for values in cases:
            with self.subTest(values=values):
                self.env = dict(original, **values)
                self.run_archive(success=False)
                self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
