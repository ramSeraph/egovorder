import contextlib
import datetime
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from egovorder import sync
from egovorder.srcs import datasrcs, datasrcs_info
from egovorder.srcs.andhra import AndhraGOIR
from egovorder.srcs.baseorder import WebResponse
from egovorder.srcs.telangana import TelanganaGOIR
from egovorder.utils.file_storage import FileManager
from egovorder.utils.metainfo import MetaInfo


FIXTURES = Path(__file__).parent / "fixtures"
DATE = datetime.date(2025, 6, 1)
PDF = b"%PDF-1.4\n% offline fixture\n" + b"% government order\n" * 20 + b"%%EOF\n"
SOURCES = (("andhra_goir", AndhraGOIR), ("telangana_goir", TelanganaGOIR))


def response(body, url):
    result = WebResponse()
    result.set_webpage(body.encode() if isinstance(body, str) else body)
    result.set_response_url(url)
    result.set_srvresponse({})
    return result


class SourceTests(unittest.TestCase):
    def test_only_government_order_sources_are_enabled(self):
        expected = {"andhra_goir", "telangana_goir", "uttarakhand_gos"}
        self.assertEqual(set(datasrcs_info.srcinfos), expected)
        self.assertEqual({obj.name for obj in datasrcs.get_srcobjs([], None)}, expected)
        self.assertEqual(len(datasrcs.get_srcobjs(["andhra_goir", "andhra_goir"], None)), 1)

    def test_legacy_and_unknown_sources_are_rejected(self):
        for source in ("central", "states", "andhra_new", "telangana", "typo"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                datasrcs.get_srcobjs([source], None)

    def test_forms_keep_hidden_state_and_set_portal_specific_dates(self):
        for name, cls in SOURCES:
            with self.subTest(source=name):
                scraper = cls(name, None)
                form = dict(scraper.get_form_data((FIXTURES / f"{name}.html").read_bytes(), DATE))
                self.assertEqual(form["__VIEWSTATE"], "page-one-state")
                self.assertEqual(form["department"], "all")
                if name == "andhra_goir":
                    self.assertEqual(form["ctl00$ContentPlaceHolder1$txtfrmdate"], "01-06-2025")
                    self.assertEqual(form["ctl00$ContentPlaceHolder1$txttodate"], "01-06-2025")
                    self.assertNotIn("ctl00$ContentPlaceHolder1$BtnSearch", form)
                else:
                    self.assertEqual(form["txtfrmdate"], "01/06/2025")
                    self.assertEqual(form["txttodate"], "01/06/2025")
                    self.assertNotIn("sectddl", form)

    def test_result_metadata_and_pagination(self):
        for name, cls in SOURCES:
            with self.subTest(source=name):
                scraper = cls(name, None)
                html = (FIXTURES / f"{name}.html").read_bytes()
                orders, nextpage = scraper.parse_search_results(html, DATE, 1)
                self.assertEqual(len(orders), 1)
                order = orders[0]
                self.assertEqual(order["date"], DATE)
                self.assertEqual(order["department"], "Finance")
                self.assertEqual(order["gotype"], "Manuscript")
                self.assertEqual(order["gonum"], "42")
                self.assertEqual(order["abstract"], "Allocation & expenditure")
                self.assertIn("page2", nextpage["href"])
                self.assertIsNone(scraper.parse_search_results(html, DATE, 2)[1])

    def test_empty_results_do_not_crash(self):
        for name, cls in SOURCES:
            with self.subTest(source=name):
                self.assertEqual(
                    cls(name, None).parse_search_results(b"<html>No records</html>", DATE, 1),
                    ([], None),
                )

    def test_strict_catchup_rejects_unrecognized_pages_but_accepts_explicit_empty_results(self):
        for name, cls in SOURCES:
            with self.subTest(source=name):
                scraper = cls(name, None)
                scraper.strict_downloads = True
                with self.assertRaises(RuntimeError):
                    scraper.parse_search_results(b"<html>Portal unavailable</html>", DATE, 1)
                self.assertEqual(
                    scraper.parse_search_results(
                        b"<form action='./'>No records found</form>", DATE, 1
                    ),
                    ([], None),
                )

    def test_strict_catchup_rejects_failed_home_and_result_requests(self):
        for name, cls in SOURCES:
            for responses in (
                [None],
                [response(b"<html>Invalid form</html>", "https://portal.invalid/")],
                [response((FIXTURES / f"{name}.html").read_bytes(), "https://portal.invalid/"), None],
            ):
                with self.subTest(source=name, responses=len(responses)):
                    scraper = cls(name, None)
                    scraper.strict_downloads = True
                    with patch.object(scraper, "download_url", side_effect=responses):
                        with self.assertRaises(RuntimeError):
                            scraper.download_oneday(f"{name}/{DATE}", DATE)

    def test_strict_catchup_rejects_failed_pagination_after_partial_downloads(self):
        for name, cls in SOURCES:
            with self.subTest(source=name), tempfile.TemporaryDirectory() as temp:
                scraper = cls(name, FileManager(temp, False, False))
                scraper.strict_downloads = True
                page = (FIXTURES / f"{name}.html").read_bytes()
                responses = [
                    response(page, scraper.baseurl), response(page, scraper.baseurl),
                    response(PDF, scraper.baseurl), None,
                ]
                with patch.object(scraper, "download_url", side_effect=responses):
                    with self.assertRaises(RuntimeError):
                        scraper.download_oneday(f"{name}/{DATE}", DATE)
                self.assertTrue(list((Path(temp) / "raw" / name).rglob("*.pdf")))

    def test_strict_catchup_rejects_invalid_documents_and_download_links(self):
        for name, cls in SOURCES:
            with self.subTest(source=name), tempfile.TemporaryDirectory() as temp:
                scraper = cls(name, FileManager(temp, False, False))
                scraper.strict_downloads = True
                with patch.object(scraper, "download_url", return_value=response(b"<html>Error</html>", scraper.baseurl)):
                    with self.assertRaises(RuntimeError):
                        scraper.save_order(f"{name}/{DATE}/bad", scraper.baseurl, MetaInfo(), min_size=162)
                self.assertEqual(list((Path(temp) / "metatags" / name).rglob("*.xml")), [])
                orders, _ = scraper.parse_search_results((FIXTURES / f"{name}.html").read_bytes(), DATE, 1)
                orders[0]["download"]["href"] = "javascript:invalid()"
                with self.assertRaises(RuntimeError):
                    scraper.download_metainfos(f"{name}/{DATE}", orders, scraper.baseurl, [], None)

    def test_single_page_without_pagination_still_parses(self):
        for name, cls in SOURCES:
            with self.subTest(source=name):
                html = (FIXTURES / f"{name}.html").read_text().split("<div id=")[0]
                orders, nextpage = cls(name, None).parse_search_results(html, DATE, 1)
                self.assertEqual(len(orders), 1)
                self.assertIsNone(nextpage)

    def test_bad_download_links_are_skipped(self):
        for name, cls in SOURCES:
            with self.subTest(source=name):
                scraper = cls(name, None)
                html = (FIXTURES / f"{name}.html").read_bytes()
                orders, _ = scraper.parse_search_results(html, DATE, 1)
                orders[0]["download"]["href"] = "javascript:invalid()"
                self.assertEqual(
                    scraper.download_metainfos(f"{name}/{DATE}", orders, scraper.baseurl, [], None),
                    [],
                )

    def test_two_page_downloads_store_documents_and_metadata(self):
        for name, cls in SOURCES:
            with self.subTest(source=name), tempfile.TemporaryDirectory() as temp:
                storage = FileManager(temp, False, False)
                scraper = cls(name, storage)
                page_one = (FIXTURES / f"{name}.html").read_text()
                page_two = page_one.replace("page-one-state", "page-two-state")
                page_two = page_two.replace("12345", "12346").replace("42", "43")
                responses = [
                    response(page_one, scraper.baseurl),
                    response(page_one, scraper.baseurl),
                    response(PDF, scraper.baseurl),
                    response(page_two, scraper.baseurl),
                    response(PDF, scraper.baseurl),
                ]
                with patch.object(scraper, "download_url", side_effect=responses) as fetch:
                    paths = scraper.download_oneday(f"{name}/{DATE}", DATE)
                expected_ids = ("1-12345", "1-12346") if name == "andhra_goir" else (
                    "finance-manuscript-42", "finance-manuscript-43",
                )
                self.assertEqual(paths, [f"{name}/{DATE}/{docid}" for docid in expected_ids])
                for path, number in zip(paths, ("42", "43")):
                    self.assertEqual(Path(storage.get_rawfile_path(path)).read_bytes(), PDF)
                    metadata = storage.get_metainfo(path)
                    self.assertIsInstance(metadata, MetaInfo)
                    self.assertEqual(metadata["date"], DATE)
                    self.assertEqual(metadata["gonum"], number)
                    self.assertIn(scraper.hostname, metadata["url"])
                paging_form = dict(fetch.call_args_list[3].kwargs["postdata"])
                self.assertEqual(paging_form["__EVENTTARGET"], "page2")
                if name == "telangana_goir":
                    final_form = dict(fetch.call_args_list[4].kwargs["postdata"])
                    self.assertEqual(final_form["__VIEWSTATE"], "page-two-state")
                    self.assertEqual(final_form["__EVENTTARGET"], "FileMoveList2$ctl02$English")
                else:
                    self.assertIn("gid=12346&fileType=1", fetch.call_args_list[4].args[0])

    def test_telangana_rejects_html_as_a_document(self):
        scraper = TelanganaGOIR("telangana_goir", None)
        self.assertTrue(scraper.is_valid_order(PDF, 0))
        self.assertFalse(scraper.is_valid_order(b"<!doctype html><html>Error</html>", 0))
        self.assertFalse(scraper.is_valid_order(PDF, len(PDF) + 1))

    def test_telangana_not_issued_placeholder_does_not_block_following_pages(self):
        for abstract in ("Not Issued", " not\n issued ", "NOT ISSUED."):
            with self.subTest(abstract=abstract), tempfile.TemporaryDirectory() as temp:
                storage = FileManager(temp, False, False)
                scraper = TelanganaGOIR("telangana_goir", storage)
                scraper.strict_downloads = True
                fixture = (FIXTURES / "telangana_goir.html").read_text()
                page_one = fixture.replace("Allocation &amp; expenditure", abstract)
                page_two = fixture.replace("page-one-state", "page-two-state").replace("42", "43")
                responses = [
                    response(page_one, scraper.baseurl), response(page_one, scraper.baseurl),
                    response(page_two, scraper.baseurl), response(PDF, scraper.baseurl),
                ]
                relpath = f"telangana_goir/{DATE}"
                with patch.object(scraper, "download_url", side_effect=responses) as fetch:
                    paths = scraper.download_oneday(relpath, DATE)
                self.assertEqual(paths, [f"{relpath}/finance-manuscript-43"])
                self.assertEqual(fetch.call_count, 4)
                self.assertIsNone(storage.get_rawfile_path(f"{relpath}/finance-manuscript-42"))
                self.assertIsNone(storage.get_metainfo(f"{relpath}/finance-manuscript-42"))
                self.assertEqual(Path(storage.get_rawfile_path(paths[0])).read_bytes(), PDF)

    def test_telangana_does_not_skip_subjects_that_mention_not_issued(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = FileManager(temp, False, False)
            scraper = TelanganaGOIR("telangana_goir", storage)
            scraper.strict_downloads = True
            fixture = (FIXTURES / "telangana_goir.html").read_bytes()
            orders, _ = scraper.parse_search_results(fixture, DATE, 1)
            orders[0]["abstract"] = "Certificates not issued to applicants - Instructions"
            with patch.object(scraper, "download_url", return_value=response(PDF, scraper.baseurl)) as fetch:
                paths = scraper.download_metainfos(
                    f"telangana_goir/{DATE}", orders, scraper.baseurl,
                    scraper.get_form_data(fixture, DATE), None,
                )
            fetch.assert_called_once()
            self.assertEqual(paths, [f"telangana_goir/{DATE}/finance-manuscript-42"])

    def test_source_http_500_and_502_retry_with_a_delay(self):
        for code in (500, 502):
            with self.subTest(code=code):
                scraper = TelanganaGOIR("telangana_goir", None)
                scraper.strict_downloads = True
                failed = WebResponse()
                failed.set_error(HTTPError(scraper.baseurl, code, "Server error", {}, None))
                success = response(PDF, scraper.baseurl)
                with patch.object(scraper, "download_url_onetime", side_effect=[failed, success]) as fetch:
                    with patch("egovorder.srcs.baseorder.time.sleep") as sleep:
                        self.assertIs(scraper.download_url(scraper.baseurl), success)
                self.assertEqual(fetch.call_count, 2)
                sleep.assert_called_once_with(scraper.retry_delay_base_secs)

    def test_archive_identifiers_and_sandbox_namespace_are_preserved(self):
        cases = {
            "andhra_goir/2025-06-01/1-12345": "in.gov.andhra.goir.2025-06-01.1-12345",
            "telangana_goir/2025-06-01/finance-manuscript-42":
                "in.gov.telangana.goir.2025-06-01.finance-manuscript-42",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(datasrcs_info.get_identifier(path, MetaInfo(), False), expected)
                self.assertEqual(datasrcs_info.get_identifier(path, MetaInfo(), True), "egaztest." + expected)

    def test_known_archive_items_skip_document_downloads(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = FileManager(temp, False, False)
            scraper = AndhraGOIR("andhra_goir", storage)
            relurl = "andhra_goir/2025-06-01/1-12345"
            identifier = "in.gov.andhra.goir.2025-06-01.1-12345"
            scraper.set_done_checker(sync.DoneChecker({identifier}))
            with patch.object(scraper, "download_url") as fetch:
                self.assertFalse(scraper.save_order(relurl, scraper.baseurl, MetaInfo()))
            fetch.assert_not_called()
            self.assertIsNone(storage.get_rawfile_path(relurl))

    def test_existing_local_documents_are_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = FileManager(temp, False, False)
            scraper = AndhraGOIR("andhra_goir", storage)
            relurl = "andhra_goir/2025-06-01/1-12345"
            metadata = MetaInfo()
            metadata.set_date(DATE)
            with patch.object(scraper, "download_url", return_value=response(PDF, scraper.baseurl)):
                self.assertTrue(scraper.save_order(relurl, scraper.baseurl, metadata))
            with patch.object(scraper, "download_url") as fetch:
                self.assertFalse(scraper.save_order(relurl, scraper.baseurl, metadata))
            fetch.assert_not_called()


class CommandTests(unittest.TestCase):
    def test_download_command_defaults_to_all_sources(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "egovorder.sync.download.parallel_download", return_value=True
        ) as run:
            self.assertEqual(sync.main(["-D", temp, "-T", "01-06-2025"]), 0)
            args = run.call_args.args
            self.assertEqual([obj.name for obj in args[0]], ["andhra_goir", "telangana_goir", "uttarakhand_gos"])
            self.assertEqual(args[2:4], (datetime.datetime(2025, 6, 1),) * 2)

    def test_failed_worker_makes_the_command_fail(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "egovorder.sync.download.parallel_download", return_value=False
        ):
            self.assertEqual(sync.main(["-D", temp, "-s", "telangana_goir"]), 1)

    def test_bad_source_and_date_ranges_fail_before_downloading(self):
        for options in (
            ["-s", "telangana"],
            ["-t", "31-02-2025"],
            ["-t", "02-06-2025", "-T", "01-06-2025"],
            ["-d", "-1"],
        ):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    sync.main(["-D", temp, *options])
                self.assertEqual(error.exception.code, 2)
                self.assertFalse((Path(temp) / "raw").exists())


if __name__ == "__main__":
    unittest.main()
