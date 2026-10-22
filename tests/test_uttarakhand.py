import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from egovorder.sync import DoneChecker
from egovorder.srcs.datasrcs_info import get_identifier, srcinfos
from egovorder.srcs.uttarakhand import UttarakhandGO
from egovorder.utils.file_storage import FileManager
from test_goir import PDF, response


DATE = datetime.date(2025, 12, 11)
RELPATH = f"uttarakhand_gos/{DATE}"
DOCID = "PDF-37937-060226165406"
IDENTIFIER = f"in.gov.uttarakhand.go.{DATE}.{DOCID}"
FIXTURE = Path(__file__).parent / "fixtures" / "uttarakhand_gos.json"


class UttarakhandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = FileManager(self.temp.name, False, False)
        self.scraper = UttarakhandGO("uttarakhand_gos", self.storage)
        self.scraper.strict_downloads = True
        self.rows = json.loads(FIXTURE.read_text())

    def responses(self, rows):
        return [
            response("<html>Search Government Orders</html>", self.scraper.baseurl),
            response(json.dumps(rows), self.scraper.baseurl),
        ]

    def test_live_record_download_preserves_identifier_and_hindi_metadata(self):
        responses = self.responses(self.rows) + [response(PDF, self.scraper.baseurl)]
        with patch.object(self.scraper, "download_url", side_effect=responses) as fetch:
            paths = self.scraper.download_oneday(RELPATH, DATE)
        self.assertEqual(paths, [f"{RELPATH}/{DOCID}"])
        self.assertEqual(Path(self.storage.get_rawfile_path(paths[0])).read_bytes(), PDF)
        metadata = self.storage.get_metainfo(paths[0])
        self.assertEqual(metadata['subject'], self.rows[0]['Subject'])
        self.assertEqual(metadata['gonum'], self.rows[0]['GONo'])
        self.assertEqual(metadata['gotype'], 'New')
        self.assertEqual(metadata['date'], DATE)
        self.assertEqual(get_identifier(paths[0], metadata, False), IDENTIFIER)
        archive = metadata.get_ia_metadata(srcinfos['uttarakhand_gos'], False)
        self.assertEqual(archive['collection'], 'uttarakhand-gos')
        self.assertEqual(archive['language'], ['eng', 'hin'])
        self.assertIn(self.rows[0]['Subject'], archive['description'])
        search = fetch.call_args_list[1]
        self.assertEqual(search.args[0], 'https://go.uk.gov.in/en/Search/SearchGO')
        self.assertEqual(json.loads(search.kwargs['postdata'])['fromdate'], '2025-12-11')
        self.assertEqual(json.loads(search.kwargs['postdata'])['todate'], '2025-12-11')
        self.assertFalse(search.kwargs['encodepost'])
        self.assertEqual(search.kwargs['headers']['Content-Type'], 'application/json')
        self.assertIs(search.kwargs['loadcookies'], fetch.call_args_list[0].kwargs['savecookies'])
        self.assertEqual(fetch.call_args_list[2].args[0],
                         'https://go.uk.gov.in/GOFolder/PDF/37937-060226165406.pdf')

    def test_word_fallback_and_nullable_metadata(self):
        self.rows[0].update(FilePath=None, File_Path_Word='/GOFolder/DOC/order.doc',
                            G0Type='A', SectionNameE=None, CategoryNameE=None)
        responses = self.responses(self.rows) + [response(PDF, self.scraper.baseurl)]
        with patch.object(self.scraper, 'download_url', side_effect=responses) as fetch:
            paths = self.scraper.download_oneday(RELPATH, DATE)
        self.assertEqual(paths, [f'{RELPATH}/order'])
        metadata = self.storage.get_metainfo(paths[0])
        self.assertEqual(metadata['gotype'], 'Amendment')
        self.assertIn('Amendment', metadata.get_ia_metadata(srcinfos['uttarakhand_gos'], False)['title'])
        self.assertEqual(fetch.call_args_list[2].args[0], 'https://go.uk.gov.in/GOFolder/DOC/order.doc')

    def test_legacy_absolute_and_backslash_filenames_remain_compatible(self):
        for original, expected_id, expected_url in (
            ('http://go.uk.gov.in/goentry/go_letters/old-order.pdf', 'old-order',
             'https://go.uk.gov.in/goentry/go_letters/old-order.pdf'),
            ('/GOFolder/PDF\\37937-060226165406.pdf', DOCID,
             'https://go.uk.gov.in/GOFolder/PDF/37937-060226165406.pdf'),
            ('/GOFolder/PDF/37773-I00GOUK0000398099.pdf', '37773-I00GOUK0000398099',
             'https://go.uk.gov.in/GOFolder/PDF/37773-I00GOUK0000398099.pdf'),
        ):
            with self.subTest(original=original):
                self.assertEqual(self.scraper.document_location(original), (expected_id, expected_url))

    def test_archived_record_is_not_downloaded_again(self):
        self.scraper.set_done_checker(DoneChecker({IDENTIFIER}))
        with patch.object(self.scraper, 'download_url', side_effect=self.responses(self.rows)) as fetch:
            self.assertEqual(self.scraper.download_oneday(RELPATH, DATE), [])
        self.assertEqual(fetch.call_count, 2)
        self.assertIsNone(self.storage.get_rawfile_path(f'{RELPATH}/{DOCID}'))

    def test_empty_list_is_a_successful_empty_day(self):
        with patch.object(self.scraper, 'download_url', side_effect=self.responses([])):
            self.assertEqual(self.scraper.download_oneday(RELPATH, DATE), [])

    def test_failed_requests_and_invalid_json_stop_catchup(self):
        for responses in (
            [None], [response('<html>Search</html>', self.scraper.baseurl), None],
            self.responses({'error': 'Unavailable'}), self.responses(None),
            [response('<html>Search</html>', self.scraper.baseurl),
             response('<html>Server error</html>', self.scraper.baseurl)],
        ):
            with self.subTest(responses=responses):
                with patch.object(self.scraper, 'download_url', side_effect=responses):
                    with self.assertRaises(RuntimeError):
                        self.scraper.download_oneday(RELPATH, DATE)

    def test_missing_links_or_mismatched_dates_stop_catchup(self):
        for change in ({'FilePath': '', 'File_Path_Word': ''}, {'GODate1678': '12-11-2025'}):
            with self.subTest(change=change):
                rows = [dict(self.rows[0], **change)]
                with patch.object(self.scraper, 'download_url', side_effect=self.responses(rows)):
                    with self.assertRaises(RuntimeError):
                        self.scraper.download_oneday(RELPATH, DATE)
        self.assertIsNone(self.storage.get_metainfo(f'{RELPATH}/{DOCID}'))

    def test_html_download_is_not_archived_as_an_order(self):
        responses = self.responses(self.rows) + [response('<html>Not found</html>', self.scraper.baseurl)]
        with patch.object(self.scraper, 'download_url', side_effect=responses):
            with self.assertRaises(RuntimeError):
                self.scraper.download_oneday(RELPATH, DATE)
        self.assertIsNone(self.storage.get_rawfile_path(f'{RELPATH}/{DOCID}'))
        self.assertIsNone(self.storage.get_metainfo(f'{RELPATH}/{DOCID}'))

    def test_missing_document_notice_is_skipped_and_following_orders_download(self):
        notice = (FIXTURE.parent / 'uttarakhand_missing_document.xml').read_bytes()
        for strict in (False, True):
            with self.subTest(strict=strict):
                self.scraper.strict_downloads = strict
                next_id = f'next-order-{strict}'
                rows = self.rows + [dict(self.rows[0], FilePath=f'/GOFolder/PDF/{next_id}.pdf')]
                responses = self.responses(rows) + [response(notice, self.scraper.baseurl),
                                                   response(PDF, self.scraper.baseurl)]
                with patch.object(self.scraper, 'download_url', side_effect=responses):
                    paths = self.scraper.download_oneday(RELPATH, DATE)
                self.assertIsNone(self.storage.get_rawfile_path(f'{RELPATH}/{DOCID}'))
                self.assertIsNone(self.storage.get_metainfo(f'{RELPATH}/{DOCID}'))
                self.assertEqual(paths, [f'{RELPATH}/{next_id}'])
                self.assertEqual(Path(self.storage.get_rawfile_path(paths[0])).read_bytes(), PDF)

    def test_other_xml_errors_are_not_treated_as_missing_documents(self):
        notice = (FIXTURE.parent / 'uttarakhand_missing_document.xml').read_bytes()
        for body in (notice.replace(b'404', b'500'),
                     notice.replace(b'Document Not Found', b'Access denied'),
                     notice.replace(b'WSException', b'OtherResponse')):
            with self.subTest(body=body):
                responses = self.responses(self.rows) + [response(body, self.scraper.baseurl)]
                with patch.object(self.scraper, 'download_url', side_effect=responses):
                    with self.assertRaises(RuntimeError):
                        self.scraper.download_oneday(RELPATH, DATE)
                self.assertIsNone(self.storage.get_rawfile_path(f'{RELPATH}/{DOCID}'))
                self.assertIsNone(self.storage.get_metainfo(f'{RELPATH}/{DOCID}'))


if __name__ == '__main__':
    unittest.main()
