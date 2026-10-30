"""Read-only portal and document checks using the daily jobs' download code."""

import argparse
from datetime import date
import json
import logging
import os
from pathlib import Path
import signal
import time
import urllib.request

from .srcs.andhra import AndhraGOIR
from .srcs.telangana import TelanganaGOIR
from .srcs.uttarakhand import UttarakhandGO
from .utils import ext_ops
from .utils.tls import GoirHTTPSHandler


# Known nonempty dates from successful archive runs. Every route checks the
# same dates; an empty day's results must not count as a working download.
SOURCES = {
    "andhra_goir": (AndhraGOIR, date(2026, 3, 25)),
    "telangana_goir": (TelanganaGOIR, date(2026, 10, 29)),
    "uttarakhand_gos": (UttarakhandGO, date(2026, 10, 27)),
}
METHODS = ("direct", "configured", "gcp")
RESULTS = Path("proxy-check-results")


class SampleDownloaded(Exception):
    pass


class CheckTimedOut(BaseException):
    # Escape the downloader's per-request exception handling as well.
    pass


class SampleStorage:
    """Inspect a single valid document in memory, without saving or uploading."""

    def __init__(self):
        self.candidates = 0
        self.sample = None

    def should_download_raw(self, relurl, url, validurl=True):
        self.candidates += 1
        if self.candidates > 5:
            raise RuntimeError("No valid document among the first five candidates")
        return True

    def save_rawdoc(self, source, relurl, headers, document):
        mime = ext_ops.get_buffer_type(document)
        if mime not in (
            "application/pdf", "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ):
            raise RuntimeError(f"Document returned {mime}, not a PDF or Word file")
        self.sample = {"id": relurl, "bytes": len(document), "mime": mime}
        raise SampleDownloaded


def timed_out(signum, frame):
    raise CheckTimedOut("Check exceeded three minutes")


def redact(value):
    value = str(value)
    for name in ("PROXY_AUTH", "PROXY_IP_ADDRESS"):
        secret = os.environ.get(name)
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def check(source, method, proxy_url):
    cls, day = SOURCES[source]
    storage = SampleStorage()
    scraper = cls(source, storage)
    scraper.strict_downloads = True
    scraper.request_timeout_secs = 25
    scraper.num_http_retries = 2
    scraper.retry_delay_base_secs = 2
    # Override both environment and per-host proxy defaults, including macOS
    # system proxies. Direct means direct, and each proxy test uses only itself.
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    scraper._url_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies), GoirHTTPSHandler(),
    )
    requests = []
    original_fetch = scraper.download_url_onetime

    def fetch(url, loadcookies, savecookies, postdata, referer, encodepost, headers, verb):
        response = original_fetch(
            url, loadcookies, savecookies, postdata, referer, encodepost, headers, verb,
        )
        requests.append({
            "url": url, "verb": verb or ("POST" if postdata else "GET"),
            "bytes": len(response.webpage or b""),
            "error": redact(response.error) if response.error else None,
        })
        return response

    scraper.download_url_onetime = fetch
    started = time.monotonic()
    result = {"source": source, "method": method, "date": str(day), "passed": False}
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(180)
    try:
        scraper.download_oneday(f"{source}/{day}", day)
        result["error"] = "No downloadable document found on the sample date"
    except SampleDownloaded:
        result["passed"] = True
        result["document"] = storage.sample
    except (CheckTimedOut, Exception) as exc:
        result["error"] = redact(exc)
        if requests and requests[-1]["error"]:
            result["error"] += ": " + requests[-1]["error"]
    finally:
        signal.alarm(0)
    result["listing_passed"] = storage.candidates > 0
    result["seconds"] = round(time.monotonic() - started, 1)
    result["requests"] = requests
    return result


def summarize():
    records = {}
    for method in METHODS:
        path = RESULTS / f"{method}.json"
        if path.exists():
            records.update({(r["source"], method): r for r in json.loads(path.read_text())})
    lines = [
        "# Proxy check", "",
        "A pass requires a parsed order listing and a valid PDF/Word download.", "",
        "| State | Direct | PROXY_IP_ADDRESS | GCP |", "|---|---|---|---|",
    ]
    for source in SOURCES:
        cells = []
        for method in METHODS:
            record = records.get((source, method), {})
            cells.append("PASS" if record.get("passed") else "FAIL" if record else "NOT RUN")
        lines.append(f"| {source} | {' | '.join(cells)} |")
    lines.append("")
    for (source, method), record in records.items():
        detail = record.get("error") or (
            f"{record['document']['id']}: {record['document']['bytes']:,} bytes"
        )
        lines.append(f"- {source} / {method}: {detail}")
    report = "\n".join(lines) + "\n"
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=METHODS)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.summary:
        summarize()
        return
    if not args.method:
        parser.error("Specify --method or --summary")
    # A NO_PROXY setting must not silently bypass the proxy under test.
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)
    logging.basicConfig(level=logging.CRITICAL)
    host = os.environ.get("PROXY_IP_ADDRESS", "")
    auth = os.environ.get("PROXY_AUTH", "")
    proxy_url = f"http://{auth + '@' if auth else ''}{host}:8080"
    records = []
    for source in SOURCES:
        if args.method != "direct" and not host:
            record = {"source": source, "method": args.method, "passed": False,
                      "error": "Proxy not configured or failed to start"}
        else:
            record = check(source, args.method, None if args.method == "direct" else proxy_url)
        records.append(record)
        print(json.dumps(record), flush=True)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{args.method}.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
