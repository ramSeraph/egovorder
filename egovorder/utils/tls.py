"""Host-specific TLS compatibility for government-order portals."""

import http.client
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
import warnings

import requests
from urllib3.exceptions import InsecureRequestWarning

GOIR_HOSTS = {"goir.ap.gov.in", "goir.telangana.gov.in"}
# OpenSSL's SSL_OP_LEGACY_SERVER_CONNECT; Python exposes the name from 3.12.
LEGACY_SERVER_CONNECT = getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4)
logger = logging.getLogger(__name__)


def expired_certificate(error):
    """Recognize expiry through urllib/urllib3's nested exception wrappers."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "verify_code", None) == 10:
            return True
        if "certificate has expired" in str(current).lower():
            return True
        if isinstance(current, BaseException):
            pending.extend(current.args)
            pending.extend(getattr(current, attr, None) for attr in ("reason", "__cause__", "__context__"))
    return False


class GoirHTTPAdapter(requests.adapters.HTTPAdapter):
    def __init__(self):
        super().__init__()
        self.expired_hosts = set()

    def send(self, request, **kwargs):
        host = urllib.parse.urlsplit(request.url).hostname
        if host not in self.expired_hosts:
            try:
                return super().send(request, **kwargs)
            except requests.exceptions.SSLError as exc:
                if host not in GOIR_HOSTS or not expired_certificate(exc):
                    raise
                logger.warning("Ignoring expired TLS certificate for %s", host)
                self.expired_hosts.add(host)
        kwargs["verify"] = False
        # Redirects use the adapter for their own destination.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            return super().send(request, **kwargs)


def configure_goir_tls(session):
    for host in GOIR_HOSTS:
        session.mount(f"https://{host}/", GoirHTTPAdapter())
    return session


class GoirHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())
        self.uttarakhand_context = ssl.create_default_context()
        self.uttarakhand_context.options |= LEGACY_SERVER_CONNECT
        self.expired_hosts = set()
        self.expired_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.expired_context.check_hostname = False
        self.expired_context.verify_mode = ssl.CERT_NONE

    def https_open(self, request):
        host = urllib.parse.urlsplit(request.full_url).hostname
        context = self.uttarakhand_context if host == 'go.uk.gov.in' else self._context
        if host not in self.expired_hosts:
            try:
                return self.do_open(http.client.HTTPSConnection, request, context=context)
            except urllib.error.URLError as exc:
                if host not in GOIR_HOSTS or not expired_certificate(exc):
                    raise
                logger.warning("Ignoring expired TLS certificate for %s", host)
                self.expired_hosts.add(host)
        return self.do_open(http.client.HTTPSConnection, request, context=self.expired_context)
