from email.message import Message
from io import BytesIO
import ssl
import unittest
import urllib.error
import urllib.request
import urllib.response
from unittest.mock import patch

import requests

from egovorder.srcs.andhra import AndhraGOIR
from egovorder.srcs.uttarakhand import UttarakhandGO
from egovorder.utils.tls import GOIR_HOSTS, LEGACY_SERVER_CONNECT, GoirHTTPSHandler, configure_goir_tls


def expired():
    error = ssl.SSLCertVerificationError("certificate has expired")
    error.verify_code = 10
    return error


def response(url, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    result = urllib.response.addinfourl(BytesIO(b"source response"), headers, url, 302 if location else 200)
    result.msg = "Found" if location else "OK"
    return result


class RequestsTlsTests(unittest.TestCase):
    def test_expiry_is_handled_automatically_for_both_goir_sources(self):
        for host in GOIR_HOSTS:
            with self.subTest(host=host):
                session = configure_goir_tls(requests.Session())
                self.addCleanup(session.close)
                with patch.object(requests.adapters.HTTPAdapter, "send", side_effect=[
                    requests.exceptions.SSLError(expired()), requests.Response(), requests.Response(),
                ]) as send:
                    session.get(f"https://{host}/")
                    session.get(f"https://{host}/dgo.ashx")
                self.assertEqual([call.kwargs["verify"] for call in send.call_args_list], [True, False, False])

    def test_other_certificate_failures_are_not_ignored(self):
        session = configure_goir_tls(requests.Session())
        self.addCleanup(session.close)
        with patch.object(requests.adapters.HTTPAdapter, "send", side_effect=requests.exceptions.SSLError("hostname mismatch")) as send:
            with self.assertRaises(requests.exceptions.SSLError):
                session.get("https://goir.ap.gov.in/")
        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["verify"])

    def test_redirects_and_other_hosts_keep_certificate_verification(self):
        session = configure_goir_tls(requests.Session())
        self.addCleanup(session.close)
        sent = []
        origin = "https://goir.ap.gov.in/"

        def send(adapter, request, **kwargs):
            sent.append((request.url, kwargs["verify"]))
            if request.url == origin and kwargs["verify"]:
                raise requests.exceptions.SSLError(expired())
            result = requests.Response()
            result.request, result.url, result._content, result.status_code = request, request.url, b"", 200
            if request.url == origin:
                result.status_code = 302
                result.headers["Location"] = "https://archive.org/"
            return result

        with patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=send):
            session.get(origin)
            session.get("https://goir.ap.gov.in.example.org/")
            session.get("https://s3.us.archive.org/")
        self.assertEqual(sent, [
            (origin, True), (origin, False), ("https://archive.org/", True),
            ("https://goir.ap.gov.in.example.org/", True), ("https://s3.us.archive.org/", True),
        ])


class UrllibTlsTests(unittest.TestCase):
    def test_uttarakhand_legacy_connection_keeps_verification_and_is_host_scoped(self):
        scraper = UttarakhandGO('uttarakhand_gos', None)
        sent = []

        def open(handler, connection, request, **kwargs):
            context = kwargs['context']
            is_uttarakhand = request.full_url == scraper.baseurl
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            if is_uttarakhand:
                self.assertTrue(context.options & LEGACY_SERVER_CONNECT)
                self.assertIs(context, handler.uttarakhand_context)
            else:
                self.assertIs(context, handler._context)
            sent.append(request.full_url)
            return response(request.full_url, 'https://archive.org/' if is_uttarakhand else None)

        with patch.object(GoirHTTPSHandler, 'do_open', autospec=True, side_effect=open):
            for url in (scraper.baseurl, 'https://go.uk.gov.in.example.org/'):
                result = scraper.download_url_onetime(url, None, None, None, None, True, {}, None)
                self.assertIsNone(result.error)
        self.assertEqual(sent, [scraper.baseurl, 'https://archive.org/', 'https://go.uk.gov.in.example.org/'])

    def test_expiry_is_handled_automatically_for_both_goir_sources(self):
        for host in GOIR_HOSTS:
            with self.subTest(host=host):
                handler = GoirHTTPSHandler()
                req = urllib.request.Request(f"https://{host}/")
                with patch.object(handler, "do_open", side_effect=[urllib.error.URLError(expired()), object(), object()]) as open:
                    handler.https_open(req)
                    handler.https_open(req)
                self.assertEqual([call.kwargs["context"].verify_mode for call in open.call_args_list],
                                 [ssl.CERT_REQUIRED, ssl.CERT_NONE, ssl.CERT_NONE])

    def test_other_certificate_failures_and_external_expiry_are_not_ignored(self):
        for url, reason in (("https://goir.ap.gov.in/", ssl.SSLCertVerificationError("hostname mismatch")),
                            ("https://go.uk.gov.in/", expired()),
                            ("https://go.uk.gov.in/", ssl.SSLCertVerificationError("hostname mismatch")),
                            ("https://archive.org/", expired()),
                            ("https://goir.ap.gov.in.example.org/", expired())):
            with self.subTest(url=url):
                handler = GoirHTTPSHandler()
                with patch.object(handler, "do_open", side_effect=urllib.error.URLError(reason)) as open:
                    with self.assertRaises(urllib.error.URLError):
                        handler.https_open(urllib.request.Request(url))
                open.assert_called_once()
                self.assertEqual(open.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)

    def test_actual_downloader_follows_redirect_with_verification_restored(self):
        scraper = AndhraGOIR("andhra_goir", None)
        sent = []
        def open(handler, connection, request, **kwargs):
            mode = kwargs["context"].verify_mode
            sent.append((request.full_url, mode))
            if request.full_url == scraper.baseurl:
                if mode == ssl.CERT_REQUIRED:
                    raise urllib.error.URLError(expired())
                return response(request.full_url, "https://archive.org/")
            return response(request.full_url)

        with patch.object(GoirHTTPSHandler, "do_open", autospec=True, side_effect=open):
            result = scraper.download_url_onetime(scraper.baseurl, None, None, None, None, True, {}, None)
        self.assertIsNone(result.error)
        self.assertEqual(result.webpage, b"source response")
        self.assertEqual(sent, [(scraper.baseurl, ssl.CERT_REQUIRED), (scraper.baseurl, ssl.CERT_NONE),
                                ("https://archive.org/", ssl.CERT_REQUIRED)])


if __name__ == "__main__":
    unittest.main()
