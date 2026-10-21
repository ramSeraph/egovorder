from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import requests

from egovorder import backfill_portal as portal


def page(gid="42", count=1, pagination="", download=True):
    link = f'<a href=\'javascript:downloadFile("{gid}", "E");\'>Download</a>' if download else ""
    return f"""<html><body><form action="./">
      <input name="__VIEWSTATE" value="saved-state">
      <input name="__EVENTTARGET"><input name="__EVENTARGUMENT">
      <input name="ctl00$ContentPlaceHolder1$txtfrmdate">
      <input name="ctl00$ContentPlaceHolder1$txttodate">
      <select name="ctl00$ContentPlaceHolder1$ddlPages"><option selected="selected">100</option></select>
      </form><span id="lblCount">{count}</span><table id="FileMoveList2">
      <tr><th>S.No</th><th>Organization</th><th>Section</th><th>G.O N.O</th><th>Date</th>
      <th>G.O Category</th><th>Abstract</th><th>Amount in G.O(₹)</th><th>ENGLISH G.O</th></tr>
      <tr><td>1</td><td>GAD01 - GENERAL ADMINISTRATION</td><td>TEST</td><td>RT-{gid}</td>
      <td>19/12/2018</td><td>Others</td><td>Offline test document</td><td>0</td><td>{link}</td></tr>
      </table><div id="divpagination">{pagination}</div></body></html>""".encode()


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.portal = portal.AndhraPortal(date(2018, 12, 19))
        self.addCleanup(self.portal.session.close)

    def test_existing_scraper_parses_live_style_rows_and_source_ids(self):
        rows, total, nextpage = self.portal.parse_rows(page())
        self.assertEqual(total, 1)
        self.assertIsNone(nextpage)
        self.assertEqual(rows[0]["identifier"], "in.gov.andhra.goir.2018-12-19.E-42")
        self.assertEqual(portal.key(rows[0]), ("2018-12-19", "generaladministration", "routine", "42"))
        self.assertEqual(rows[0]["abstract"], "Offline test document")

    def test_missing_download_link_remains_a_matchable_source_row(self):
        row = self.portal.parse_rows(page(download=False))[0][0]
        self.assertIsNone(row["identifier"])
        self.assertIsNone(row["href"])
        self.assertEqual(row["gonum"], "42")
        self.assertEqual(row["gotype"], "Routine")

    def test_unrecognized_error_page_is_not_an_empty_result(self):
        with self.assertRaisesRegex(RuntimeError, "Unrecognized"):
            self.portal.parse_rows(b"<html>Service temporarily unavailable</html>")

    def test_unexpected_date_stops_instead_of_archiving_wrong_records(self):
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.portal.parse_rows(page().replace(b"19/12/2018", b"18/12/2018"))

    def test_incomplete_pagination_never_marks_missing_rows(self):
        with patch.object(self.portal, "first", return_value=SimpleNamespace(content=page(count=2))):
            with self.assertRaisesRegex(RuntimeError, "Incomplete source listing"):
                self.portal.listed_rows()

    def test_last_link_is_followed_when_numeric_next_link_is_missing(self):
        first = SimpleNamespace(content=page(count=2, pagination="<a href=\"javascript:__doPostBack('last','2')\">Last</a>"))
        second = SimpleNamespace(content=page(gid="43", count=2))
        with patch.object(self.portal, "first", return_value=first), \
             patch.object(portal, "request", return_value=second) as request:
            rows = self.portal.listed_rows()
        self.assertEqual([r["gonum"] for r in rows], ["42", "43"])
        self.assertEqual(request.call_args.kwargs["data"]["__EVENTTARGET"], "last")
        self.assertEqual(request.call_args.kwargs["data"]["__EVENTARGUMENT"], "2")

    def test_repeated_page_stops(self):
        first = SimpleNamespace(content=page(count=2, pagination="<a href=\"javascript:__doPostBack('last','2')\">Last</a>"))
        with patch.object(self.portal, "first", return_value=first), \
             patch.object(portal, "request", return_value=first):
            with self.assertRaisesRegex(RuntimeError, "Repeated source page"):
                self.portal.listed_rows()

    def test_changing_total_stops(self):
        first = SimpleNamespace(content=page(count=2, pagination="<a href=\"javascript:__doPostBack('last','2')\">Last</a>"))
        with patch.object(self.portal, "first", return_value=first), \
             patch.object(portal, "request", return_value=SimpleNamespace(content=page(gid="43", count=3))):
            with self.assertRaisesRegex(RuntimeError, "Source count changed"):
                self.portal.listed_rows()

    def test_proxy_does_not_modify_global_environment(self):
        import os
        before = dict(os.environ)
        source = portal.AndhraPortal(date(2018, 12, 19), proxy="http://proxy.invalid:8080")
        self.addCleanup(source.session.close)
        self.assertEqual(source.session.proxies["https"], "http://proxy.invalid:8080")
        self.assertFalse(source.session.trust_env)
        self.assertEqual(dict(os.environ), before)

    def test_temporary_error_page_is_retried_instead_of_recorded_as_a_gap(self):
        response = Mock(status_code=200, content=b"Unable to reach server")
        session = Mock()
        session.request.side_effect = [response, Mock(status_code=200, content=b"document")]
        with patch.object(portal.time, "sleep"):
            result = portal.request(session, "GET", portal.ANDHRA_ORIGIN)
        self.assertEqual(result.content, b"document")
        self.assertEqual(session.request.call_count, 2)

    def test_source_server_outage_gets_minutes_to_recover(self):
        failed = requests.Response()
        failed.status_code = 500
        recovered = requests.Response()
        recovered.status_code = 200
        recovered._content = b"document"
        session = Mock()
        session.request.side_effect = [failed] * 5 + [recovered]
        with patch.object(portal.time, "sleep") as sleep:
            self.assertIs(portal.request(session, "GET", portal.ANDHRA_ORIGIN), recovered)
        self.assertEqual(sleep.call_args_list, [call(n) for n in (30, 60, 120, 240, 300)])
        self.assertEqual(session.request.call_count, 6)

    def test_persistent_server_outage_remains_an_error(self):
        failed = requests.Response()
        failed.status_code = 500
        session = Mock()
        session.request.return_value = failed
        with patch.object(portal.time, "sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                portal.request(session, "GET", portal.ANDHRA_ORIGIN)
        self.assertEqual(session.request.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_budget_can_interrupt_the_longer_backoff(self):
        from egovorder.backfill import BudgetExpired
        session = Mock()
        session.request.side_effect = requests.Timeout()
        with patch.object(portal.time, "sleep", side_effect=BudgetExpired):
            with self.assertRaises(BudgetExpired):
                portal.request(session, "GET", portal.ANDHRA_ORIGIN)
        self.assertEqual(session.request.call_count, 1)

    def test_missing_document_is_not_retried_as_an_outage(self):
        response = requests.Response()
        response.status_code = 404
        session = Mock()
        session.request.return_value = response
        with patch.object(portal.time, "sleep") as sleep:
            self.assertIs(portal.request(session, "GET", portal.ANDHRA_ORIGIN, missing_ok=True), response)
        sleep.assert_not_called()

    def test_archive_requests_keep_the_existing_short_retry_policy(self):
        session = Mock()
        session.request.side_effect = requests.Timeout()
        with patch.object(portal.time, "sleep") as sleep:
            with self.assertRaises(requests.Timeout):
                portal.request(session, "GET", "https://archive.org/metadata/example")
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(6)])
        self.assertEqual(session.request.call_count, 4)


if __name__ == "__main__":
    unittest.main()
