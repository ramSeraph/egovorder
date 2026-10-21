import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ResumeScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bin = Path(self.temp.name)
        ia = self.bin / "ia"
        ia.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "if 'NOT date:*' not in sys.argv[2]:\n"
            "    print(os.environ.get('FAKE_IA_OUTPUT', ''))\n"
        )
        ia.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{Path(sys.executable).parent}:{os.environ['PATH']}")
        self.env.pop("TESTING", None)

    def run_script(self, script, *args):
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / script), *map(str, args)],
            cwd=ROOT, env=self.env, text=True, capture_output=True, check=True,
        )

    def test_no_archive_items_fall_back_to_source_start_dates(self):
        for source, expected in (("andhra_goir", "01-06-2008"), ("telangana_goir", "02-06-2014"),
                                 ("uttarakhand_gos", "13-05-1999")):
            with self.subTest(source=source):
                result = self.run_script("get_fromdate.sh", source)
                self.assertEqual(result.stdout.strip(), expected)

    def test_latest_archive_date_is_written_to_output(self):
        self.env["FAKE_IA_OUTPUT"] = '{"date":"2025-06-01T00:00:00Z"}'
        output = self.bin / "from date.txt"
        self.run_script("get_fromdate.sh", "andhra_goir", output)
        self.assertEqual(output.read_text(), "01-06-2025\n")

    def test_sandbox_done_list_uses_production_identifiers(self):
        self.env["TESTING"] = "1"
        self.env["FAKE_IA_OUTPUT"] = "egaztest.in.gov.telangana.goir.2025-06-01.finance-manuscript-42"
        output = self.bin / "done list.txt"
        self.run_script("get_donelist.sh", "telangana_goir", "01-06-2025", "07-06-2025", output)
        self.assertEqual(output.read_text(), "in.gov.telangana.goir.2025-06-01.finance-manuscript-42\n")


if __name__ == "__main__":
    unittest.main()
