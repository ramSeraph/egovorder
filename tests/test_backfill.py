from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pymupdf
import requests

from egovorder import backfill as job
from egovorder.backfill_portal import AndhraPortal
from collections import Counter
import csv
import hashlib
import json
import shutil
import os


def pdf():
    doc = pymupdf.open()
    doc.new_page()
    content = doc.tobytes()
    doc.close()
    return content


def row(number="42"):
    return {
        "identifier": f"in.gov.andhra.goir.2018-12-19.E-{number}",
        "date": "2018-12-19", "department": "GAD01 - GENERAL ADMINISTRATION",
        "gotype": "Routine", "gonum": number, "category": "Others", "section": "TEST",
        "abstract": "Offline test document", "amount": "0",
        "href": f'javascript:downloadFile("{number}", "E");', "serial": 1,
    }


class BackfillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.runner = job.Backfill.__new__(job.Backfill)
        self.runner.root = self.root
        self.runner.proxy = None
        self.runner.counts = Counter()
        self.runner.storage = job.FileManager(str(self.root / "data"), False, False)
        self.runner.access = "fake-access"
        self.runner.secret = "fake-secret"
        self.runner.ia_session = object()
        self.runner.public = Mock()
        self.runner.current_day = "2018-12-19"
        self.runner.started_at = job.now()
        self.runner.remote_item = Mock(return_value={})
        self.document = SimpleNamespace(
            status_code=200, content=pdf(), url="https://goir.ap.gov.in/dgo.ashx?gid=42&fileType=E",
            raise_for_status=Mock(),
        )
        self.portal = SimpleNamespace(url="https://goir.ap.gov.in/", session=Mock())
        self.portal.session.request.return_value = self.document
        patch.object(job.time, "sleep").start()
        self.upload = patch.object(job, "upload", return_value=[Mock(), Mock()]).start()
        self.addCleanup(patch.stopall)

    def test_pdf_validation_rejects_empty_html_and_corrupt_pdf(self):
        self.assertEqual(job.document_format(self.document.content), "pdf")
        for body in (b"", b"<html>Unavailable</html>", b"%PDF-1.4\nbroken"):
            self.assertIsNone(job.document_format(body))

    def test_source_tls_verification_is_enabled_by_default(self):
        portal = AndhraPortal(job.date(2008, 7, 8))
        with patch.object(requests.adapters.HTTPAdapter, "send", return_value=requests.Response()) as send:
            portal.session.get(portal.url)
        self.assertTrue(send.call_args.kwargs["verify"])

    def test_success_records_receipt_and_removes_only_uploaded_files(self):
        result = self.runner.process(row(), self.portal)
        self.assertEqual(result["status"], "uploaded")
        self.assertTrue(self.upload.call_args.kwargs["verify"])
        self.assertEqual(self.upload.call_args.kwargs["metadata"]["collection"], "andhra-goir")
        self.assertTrue(all(not self.runner.file_path(f).exists() for f in result["files"]))
        self.assertEqual(self.runner.outcome(row())["status"], "uploaded")

    def test_existing_item_needs_no_source_download_or_upload(self):
        self.runner.remote_item.return_value = {
            "metadata": {"identifier": row()["identifier"]},
            "files": [{"name": f"E-42.{ext}", "md5": "abc", "size": "100"} for ext in ("pdf", "xml")],
        }
        self.assertEqual(self.runner.process(row(), self.portal)["status"], "already_archived")
        self.portal.session.request.assert_not_called()
        self.upload.assert_not_called()

    def test_unavailable_is_recorded_and_next_record_can_upload(self):
        self.document.content = b"<html>No document</html>"
        self.assertEqual(self.runner.process(row(), self.portal)["status"], "unavailable")
        self.upload.assert_not_called()
        self.document.content = pdf()
        self.assertEqual(self.runner.process(row("43"), self.portal)["status"], "uploaded")

    def test_failed_upload_keeps_files_and_resumes_without_redownloading(self):
        self.upload.side_effect = requests.HTTPError("upload failed")
        with self.assertRaises(requests.HTTPError):
            self.runner.process(row(), self.portal)
        pending = self.runner.outcome(row())
        self.assertEqual(pending["status"], "downloaded")
        self.assertTrue(all(self.runner.file_path(f).is_file() for f in pending["files"]))
        self.upload.side_effect = None
        self.portal.session.request.reset_mock()
        self.assertEqual(self.runner.process(row(), self.portal)["status"], "uploaded")
        self.portal.session.request.assert_not_called()

    def test_request_failure_is_not_classified_as_an_unavailable_document(self):
        self.portal.session.request.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            self.runner.process(row(), self.portal)
        self.assertIsNone(self.runner.outcome(row()))
        self.upload.assert_not_called()

    def test_server_outage_leaves_the_record_and_day_unfinished(self):
        response = requests.Response()
        response.status_code = 500
        self.portal.session.request.return_value = response
        self.portal.listed_rows = Mock(return_value=[row()])
        with patch.object(job, "AndhraPortal", return_value=self.portal):
            with self.assertRaises(requests.HTTPError):
                self.runner.day(row()["date"], {job.key(row()): row()})
        self.assertIsNone(self.runner.outcome(row()))
        self.assertFalse((self.root / "days" / (row()["date"] + ".json")).exists())
        self.upload.assert_not_called()

    def test_day_uploads_only_candidates_and_keeps_checkpoint_on_failure(self):
        selected, other = row(), row("99")
        fake = Mock()
        fake.listed_rows.return_value = [selected, other]
        fake.session = self.portal.session
        fake.url = self.portal.url
        with patch.object(job, "AndhraPortal", return_value=fake):
            self.upload.side_effect = requests.HTTPError("upload failed")
            with self.assertRaises(requests.HTTPError):
                self.runner.day("2018-12-19", {job.key(selected): selected})
            self.assertFalse((self.root / "days/2018-12-19.json").exists())
            self.assertIsNone(self.runner.outcome(other))
            self.upload.side_effect = None
            self.runner.day("2018-12-19", {job.key(selected): selected})
        self.assertTrue((self.root / "days/2018-12-19.json").is_file())
        self.assertIsNone(self.runner.outcome(other))

    def test_timeout_reconciles_an_accepted_pdf_before_retrying_xml(self):
        def remote(identifier):
            pending = self.runner.outcome(row())
            if self.upload.call_count and pending:
                raw = pending["files"][0]
                return {"metadata": {"identifier": identifier}, "files": [raw]}
            return {}

        self.runner.remote_item.side_effect = remote
        self.upload.side_effect = [requests.ReadTimeout("accepted but response lost"), [Mock()]]
        result = self.runner.process(row(), self.portal)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(self.upload.call_count, 2)
        second_files = self.upload.call_args.args[1]
        self.assertEqual(len(second_files), 1)
        self.assertTrue(second_files[0].endswith(".xml"))
        self.assertEqual(self.portal.session.request.call_count, 1)

    def test_persistent_timeout_retains_files_and_state_is_portable(self):
        self.upload.side_effect = requests.ReadTimeout()
        with self.assertRaises(requests.ReadTimeout):
            self.runner.process(row(), self.portal)
        self.assertEqual(self.upload.call_count, 4)
        pending = self.runner.outcome(row())
        self.assertEqual(pending["status"], "downloaded")
        self.assertTrue(all(self.runner.file_path(f).is_file() for f in pending["files"]))
        moved = self.root / "restored"
        shutil.copytree(self.root / "records", moved / "records")
        shutil.copytree(self.root / "data", moved / "data")
        self.runner.root = moved
        self.upload.side_effect = None
        self.portal.session.request.reset_mock()
        with patch.object(job, "AndhraPortal") as portal:
            self.runner.day(row()["date"], {job.key(row()): row()})
        portal.assert_not_called()
        self.assertEqual(self.runner.outcome(row())["status"], "uploaded")
        self.assertTrue(all(not self.runner.file_path(f).exists() for f in pending["files"]))

    def test_conflicting_remote_bytes_are_never_overwritten(self):
        self.upload.side_effect = requests.HTTPError()
        with self.assertRaises(requests.HTTPError):
            self.runner.process(row(), self.portal)
        pending = self.runner.outcome(row())
        self.runner.remote_item.return_value = {
            "metadata": {}, "files": [{"name": pending["files"][0]["name"], "md5": "different"}],
        }
        self.upload.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "Existing IA file differs"):
            self.runner.process(row(), self.portal)
        self.upload.assert_not_called()
        self.assertTrue(all(self.runner.file_path(f).is_file() for f in pending["files"]))

    def pending_with_archived_xml(self, transform):
        self.upload.side_effect = requests.HTTPError("upload failed")
        with self.assertRaises(requests.HTTPError):
            self.runner.process(row(), self.portal)
        pending = self.runner.outcome(row())
        xml = pending["files"][1]
        archived = transform(self.runner.file_path(xml).read_bytes())
        archived_file = {"name": xml["name"], "size": str(len(archived)),
                         "md5": hashlib.md5(archived).hexdigest()}
        self.runner.remote_item.return_value = {
            "metadata": {"identifier": row()["identifier"]}, "files": [archived_file],
        }
        self.runner.public.request.return_value = SimpleNamespace(
            status_code=200, content=archived, raise_for_status=Mock(),
        )
        self.upload.reset_mock(side_effect=True)
        self.upload.return_value = [Mock()]
        self.portal.session.request.reset_mock()
        return pending, archived_file

    def test_resume_preserves_equivalent_archived_xml_and_uploads_only_pdf(self):
        pending, archived = self.pending_with_archived_xml(
            lambda data: data.replace(b"GAD01 - GENERAL", b"GAD01\n- GENERAL")
        )
        # Legacy unknown files are not sufficient evidence of a usable document.
        self.runner.remote_item.return_value["files"].append(
            {"name": "E-42.unkwn", "md5": "old-unknown-document", "size": "80"}
        )
        result = self.runner.process(row(), self.portal)
        self.assertEqual(result["status"], "uploaded")
        self.portal.session.request.assert_not_called()
        self.assertEqual(self.upload.call_args.args[1], [str(self.runner.file_path(pending["files"][0]))])
        self.assertIsNone(self.upload.call_args.kwargs["metadata"])
        self.assertEqual(result["files"][1]["md5"], archived["md5"])
        self.assertEqual(result["files"][1]["bytes"], int(archived["size"]))
        self.assertTrue(result["files"][1]["preserved_existing"])
        self.assertTrue(all(not self.runner.file_path(f).exists() for f in pending["files"]))

    def test_real_metadata_change_retains_pending_files_and_stops(self):
        pending, _ = self.pending_with_archived_xml(
            lambda data: data.replace(b"<gonum>42</gonum>", b"<gonum>43</gonum>")
        )
        with self.assertRaisesRegex(RuntimeError, "Existing IA file differs"):
            self.runner.process(row(), self.portal)
        self.upload.assert_not_called()
        self.assertEqual(self.runner.outcome(row())["status"], "downloaded")
        self.assertTrue(all(self.runner.file_path(f).is_file() for f in pending["files"]))

    def test_archived_xml_download_must_match_ia_checksum(self):
        pending, archived = self.pending_with_archived_xml(
            lambda data: data.replace(b"GAD01 - GENERAL", b"GAD01\n- GENERAL")
        )
        archived["md5"] = "different-remote-content"
        with self.assertRaisesRegex(RuntimeError, "Existing IA file differs"):
            self.runner.process(row(), self.portal)
        self.upload.assert_not_called()
        self.assertTrue(all(self.runner.file_path(f).is_file() for f in pending["files"]))

    def test_malformed_metadata_is_not_equivalent(self):
        self.assertFalse(job.same_xml_content(b"<document>broken", b"<document>broken"))

    def test_metadata_only_item_is_not_treated_as_complete(self):
        self.runner.remote_item.return_value = {"metadata": {"identifier": row()["identifier"]}}
        self.assertEqual(self.runner.process(row(), self.portal)["status"], "uploaded")
        self.upload.assert_called_once()

    def test_completed_receipt_survives_a_later_missing_source_row(self):
        self.runner.process(row(), self.portal)
        with patch.object(job, "AndhraPortal") as portal:
            self.runner.day(row()["date"], {job.key(row()): row()})
        portal.assert_not_called()
        self.assertEqual(self.runner.outcome(row())["status"], "uploaded")

    def test_gap_is_rechecked_only_when_requested(self):
        self.document.content = b"<html>No document</html>"
        self.runner.process(row(), self.portal)
        self.document.content = pdf()
        self.assertEqual(self.runner.process(row(), self.portal)["status"], "unavailable")
        self.assertEqual(self.runner.process(row(), self.portal, retry_unavailable=True)["status"], "uploaded")

    def test_ambiguous_source_rows_do_not_upload_or_complete_the_day(self):
        fake = Mock(url=self.portal.url, session=self.portal.session)
        fake.listed_rows.return_value = [row(), row()]
        with patch.object(job, "AndhraPortal", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
                self.runner.day(row()["date"], {job.key(row()): row()})
        self.upload.assert_not_called()
        self.assertFalse((self.root / "days" / (row()["date"] + ".json")).exists())

    def test_report_keeps_source_gaps_distinct_from_archived_orders(self):
        self.runner.save(row(), "unavailable", reason="http_missing")
        grouped = {row()["date"]: {job.key(row()): row(), job.key(row("99")): row("99")}}
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.root / "job-summary.md")}):
            job.write_report(self.runner, grouped, "complete")
        progress = json.loads((self.root / "progress.json").read_text())
        self.assertEqual(progress["status"], "complete_with_source_gaps")
        self.assertEqual(progress["selection"]["outcomes"], {"unavailable": 1, "not_attempted": 1})
        with (self.root / "outcomes.csv").open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 2)


class PlanTests(unittest.TestCase):
    def test_fixed_plan_and_narrow_date_selection(self):
        grouped = job.load_plan(job.PLAN)
        self.assertEqual(sum(map(len, grouped.values())), 12320)
        self.assertEqual(len(grouped), 1217)
        selected = job.load_plan(job.PLAN, job.date(2018, 10, 1), job.date(2018, 10, 31))
        self.assertEqual(sum(map(len, selected.values())), 1974)
        self.assertTrue(all(day.startswith("2018-10-") for day in selected))

    def test_parallel_partitions_cover_the_audit_exactly_once(self):
        combined = set()
        for partition, count in zip((*job.MONTHS, "other"), (1762, 1365, 1038, 1974, 1833, 1032, 3316)):
            selected = job.load_plan(job.PLAN, partition=partition)
            keys = {key for candidates in selected.values() for key in candidates}
            self.assertEqual(len(keys), count)
            self.assertFalse(combined.intersection(keys))
            self.assertTrue(all(day < job.AUDIT_END.isoformat() for day in selected))
            combined.update(keys)
        self.assertEqual(len(combined), 12320)

    def test_partitions_have_separate_state_directories(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), \
             patch.object(job, "Backfill") as runner, patch.object(job, "run_plan"), \
             patch.object(job, "write_report"):
            base = ["--upload", "--state-dir", temp]
            job.main([*base, "--partition", "2018-10"])
            job.main([*base, "--partition", "2018-11"])
        self.assertEqual([call.args[0].relative_to(temp).as_posix() for call in runner.call_args_list],
                         ["2018-10/production", "2018-11/production"])

    def test_validation_has_no_network_or_state_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            with patch.object(job, "Backfill") as runner, patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
                self.assertEqual(job.main(["--validate", "--state-dir", str(state)]), 0)
            runner.assert_not_called()
            self.assertFalse(state.exists())

    def test_github_work_budget_cannot_exceed_210_minutes(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUN_TIME_MINS": ""}):
            for args in ([], ["--minutes", "0"], ["--minutes", "211"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as exc:
                    job.main(["--validate", *args])
                self.assertEqual(exc.exception.code, 2)

    def test_reversed_range_and_no_candidates_fail_before_work(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            for start, end in (("02-10-2018", "01-10-2018"), ("01-01-2026", "02-01-2026")):
                with self.subTest(start=start), self.assertRaises(SystemExit):
                    job.main(["--validate", "--from-date", start, "--to-date", end])

    def test_key_normalizes_export_and_live_department_labels(self):
        self.assertEqual(job.key(row()), job.key(dict(row(), department="General Administration", gotype="RT")))

    def test_budget_pause_retains_pending_work_and_reports_success(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            pending_row = next(iter(job.load_plan(job.PLAN, job.date(2018, 12, 19), job.date(2018, 12, 19))["2018-12-19"].values()))
            def expire(runner, grouped, retry):
                runner.save(pending_row, "downloaded", identifier="pending-test-item")
                raise job.BudgetExpired
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "IA_ACCESS_KEY": "fake", "IA_SECRET_KEY": "fake"}), \
                 patch.object(job, "get_session", return_value=Mock()), patch.object(job, "run_plan", side_effect=expire):
                result = job.main(["--upload", "--state-dir", str(state), "--minutes", "1",
                                   "--from-date", "19-12-2018", "--to-date", "19-12-2018"])
            self.assertEqual(result, 0)
            progress = json.loads((state / "all/production/progress.json").read_text())
            self.assertEqual(progress["status"], "paused_at_time_limit")
            self.assertEqual(progress["selection"]["outcomes"]["downloaded"], 1)
            self.assertFalse((state / "all/production/days/2018-12-19.json").exists())
            self.assertEqual(len(list((state / "all/production/records").glob("*.json"))), 1)


class LatestCatchupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.runner = SimpleNamespace(root=self.root, public=object(), proxy=None, day=Mock())
        def portal(day, **kwargs):
            result = Mock()
            result.listed_rows.return_value = [dict(row(), date=day.isoformat())]
            return result
        self.portal = patch.object(job, "AndhraPortal", side_effect=portal).start()
        self.latest = patch.object(job, "latest_ia_date", return_value=job.date(2025, 12, 12)).start()
        self.addCleanup(patch.stopall)

    def test_first_run_includes_latest_archived_day_and_today(self):
        job.catch_up_latest(self.runner, today=job.date(2025, 12, 14))
        self.assertEqual([call.args[0] for call in self.runner.day.call_args_list],
                         ["2025-12-12", "2025-12-13", "2025-12-14"])
        state = json.loads((self.root / "latest.json").read_text())
        self.assertEqual(state["next_date"], "2025-12-15")
        self.assertEqual(len(job.latest_candidates(self.root)), 3)
        self.latest.assert_called_once()

    def test_timeout_resumes_unfinished_day_even_if_newer_items_exist_on_ia(self):
        self.runner.day.side_effect = [None, job.BudgetExpired]
        with self.assertRaises(job.BudgetExpired):
            job.catch_up_latest(self.runner, today=job.date(2025, 12, 14))
        self.assertEqual(json.loads((self.root / "latest.json").read_text())["next_date"], "2025-12-13")
        self.latest.side_effect = AssertionError("Must not move the start date past unfinished work")
        self.runner.day.reset_mock()
        self.runner.day.side_effect = None
        job.catch_up_latest(self.runner, today=job.date(2025, 12, 14))
        self.assertEqual([call.args[0] for call in self.runner.day.call_args_list], ["2025-12-13", "2025-12-14"])
        self.latest.assert_called_once()

    def test_failed_upload_does_not_advance_the_date(self):
        self.runner.day.side_effect = requests.HTTPError("upload failed")
        with self.assertRaises(requests.HTTPError):
            job.catch_up_latest(self.runner, today=job.date(2025, 12, 14))
        self.assertEqual(json.loads((self.root / "latest.json").read_text())["next_date"], "2025-12-12")

    def test_catchup_never_overlaps_historical_partitions_or_uses_future_baseline(self):
        for day in (job.date(2018, 10, 1), job.date(2025, 12, 15)):
            self.latest.return_value = day
            with self.subTest(day=day), self.assertRaises(ValueError):
                job.catch_up_latest(self.runner, today=job.date(2025, 12, 14))
        self.portal.assert_not_called()

    def test_retry_gaps_revisits_completed_days_without_resetting_cursor(self):
        job.catch_up_latest(self.runner, today=job.date(2025, 12, 12))
        job.atomic_json(self.root / "days/2025-12-12.json", {"outcomes": {"unavailable": 1}})
        self.runner.day.reset_mock()
        job.catch_up_latest(self.runner, retry_unavailable=True, today=job.date(2025, 12, 13))
        self.assertEqual([call.args[0] for call in self.runner.day.call_args_list], ["2025-12-12", "2025-12-13"])
        self.assertEqual(json.loads((self.root / "latest.json").read_text())["next_date"], "2025-12-14")


if __name__ == "__main__":
    unittest.main()
