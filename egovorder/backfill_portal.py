"""List Andhra audit candidates using the existing GOIR form and row parser."""

from datetime import datetime
import hashlib
import json
import re
import time

from bs4 import BeautifulSoup
import requests

from .srcs.andhra import AndhraGOIR
from .srcs.datasrcs_info import get_identifier
from .utils.tls import configure_goir_tls

SOURCE = "andhra_goir"
ANDHRA_ORIGIN = "https://goir.ap.gov.in/"
SOURCE_RETRY_DELAYS = (30, 60, 120, 240, 300)


def key(row):
    department = re.sub(r"^[a-zA-Z]{2,8}\d+(?:-[a-zA-Z])?\s*-\s*", "", row["department"].strip())
    department = re.sub(r"[^a-z0-9]", "", department.casefold())
    gotype = {"MS": "manuscript", "RT": "routine"}.get(row["gotype"], row["gotype"].lower())
    return row["date"], department, gotype, row["gonum"].strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()


def retryable(exc):
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return isinstance(exc, requests.HTTPError) and exc.response is not None and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


def request(session, method, url, *, missing_ok=False, **kwargs):
    delays = SOURCE_RETRY_DELAYS if url.startswith(ANDHRA_ORIGIN) else (2, 4, 6)
    for attempt in range(len(delays) + 1):
        try:
            response = session.request(method, url, timeout=(15, 90), **kwargs)
            if missing_ok and response.status_code in (404, 410):
                return response
            response.raise_for_status()
            if url.startswith(ANDHRA_ORIGIN) and b"Unable to reach server" in response.content:
                raise requests.ConnectionError("Portal returned its temporary server-error page")
            return response
        except requests.RequestException as exc:
            if attempt == len(delays) or not retryable(exc):
                raise
            # Exception messages can contain proxy credentials.
            reason = f"HTTP {exc.response.status_code}" if exc.response is not None else type(exc).__name__
            print(f"Retrying request after {reason}; waiting {delays[attempt]} seconds", flush=True)
            time.sleep(delays[attempt])


class AndhraPortal:
    def __init__(self, day, *, proxy=None):
        self.day = day
        self.scraper = AndhraGOIR(SOURCE, None)
        self.url = self.scraper.baseurl
        self.session = configure_goir_tls(requests.Session())
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            self.session.trust_env = False
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": self.url})
        self.prefix = "ctl00$ContentPlaceHolder1$"
        self.page_field = self.prefix + "ddlPages"
        self.page_no = 0
        self.rows = None

    def form(self, content):
        fields = self.scraper.get_form_data(content, self.day)
        if fields is None:
            raise RuntimeError("Missing Andhra search form")
        fields = dict(fields)
        fields[self.prefix + "txtfrmdate"] = self.day.strftime("%d-%m-%Y")
        fields[self.prefix + "txttodate"] = self.day.strftime("%d-%m-%Y")
        fields.update(__EVENTTARGET="", __EVENTARGUMENT="")
        return fields

    def first(self):
        home = request(self.session, "GET", self.url)
        fields = self.form(home.content)
        fields[self.prefix + "BtnSearch"] = "Search"
        result = request(self.session, "POST", self.url, data=fields)
        select = BeautifulSoup(result.content, "lxml", from_encoding="utf-8").find("select", attrs={"name": self.page_field})
        if select and self.scraper.get_selected_option(select) != "100":
            fields = self.form(result.content)
            fields[self.page_field] = "100"
            fields["__EVENTTARGET"] = self.page_field
            result = request(self.session, "POST", self.url, data=fields)
        self.page_no = 1
        return result

    def parse_rows(self, content):
        # Short final pages can be misdetected as Windows-1250, corrupting
        # the rupee sign in the amount column and rejecting valid order rows.
        soup = BeautifulSoup(content, "lxml", from_encoding="utf-8")
        table = soup.find("table", id="FileMoveList2")
        if not table:
            if self.scraper.get_search_form(content, "./") is not None and re.search(
                r"No\s+Records\s+Found", soup.get_text(" ", strip=True), re.I
            ):
                return [], 0, None
            raise RuntimeError("Unrecognized Andhra results page")
        trs = table.find_all("tr")
        order = self.scraper.get_column_order(trs[0])
        headers = [c.get_text(" ", strip=True) for c in trs[0].find_all("th")]
        date_col = headers.index("Date")
        label = soup.find(id="lblCount")
        if label is None or not re.search(r"\d+", label.get_text()):
            raise RuntimeError("Missing Andhra result count")
        total = int(re.search(r"\d+", label.get_text()).group())
        rows = []
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            day = datetime.strptime(cells[date_col][:10], "%d/%m/%Y").date()
            if day != self.day:
                raise RuntimeError("Andhra returned a record outside the requested date")
            infos = []
            self.scraper.process_result_row(tr, infos, day, order)
            if not infos and not tr.find_all("td")[order.index("download")].find("a"):
                row = {field: cells[i] for i, field in enumerate(order) if field and field != "download"}
                go = row["gonum"].split("-", 1)
                row.update(gotype={"MS": "Manuscript", "RT": "Routine"}.get(go[0], go[0]), gonum=go[-1])
                row.update(identifier=None, date=day.isoformat(), href=None)
            else:
                if len(infos) != 1:
                    raise RuntimeError("Unrecognized Andhra result row")
                info = infos[0]
                href = info.pop("download").get("href", "")
                parts = re.findall(r'"([^\"]+)"', href)
                if len(parts) < 2 or not parts[0].isdigit() or not re.fullmatch(r"[A-Za-z]+", parts[1]):
                    raise RuntimeError("Unrecognized Andhra document identifier")
                gid, kind = parts[:2]
                identifier = get_identifier(f"{SOURCE}/{day}/{kind}-{gid}", info, False)
                row = dict(info)
                row.update(identifier=identifier, date=day.isoformat(), href=href)
            rows.append(row)
        return rows, total, self.scraper.find_next_page(soup, self.page_no)

    def listed_rows(self):
        if self.rows is not None:
            return self.rows
        response = self.first()
        rows, page_hashes = [], set()
        expected = None
        while True:
            parsed, total, nextpage = self.parse_rows(response.content)
            digest = fingerprint(parsed)
            if digest in page_hashes:
                raise RuntimeError(f"Repeated source page for {self.day}")
            page_hashes.add(digest)
            rows.extend(parsed)
            expected = total if expected is None else expected
            if total != expected:
                raise RuntimeError(f"Source count changed during pagination for {self.day}")
            if not nextpage and len(rows) < total:
                paging = BeautifulSoup(response.content, "lxml", from_encoding="utf-8").find(id="divpagination")
                if paging:
                    nextpage = next((a for a in paging.find_all("a", href=True)
                                     if a.get_text(strip=True) == "Last"), None)
            if not nextpage:
                if len(rows) != expected:
                    raise RuntimeError(f"Incomplete source listing: {len(rows)}/{expected} for {self.day}")
                self.rows = rows
                return rows
            fields = self.form(response.content)
            match = re.search(r"__doPostBack\('([^']*)','([^']*)'\)", nextpage["href"])
            if not match:
                raise RuntimeError("Unrecognized Andhra pagination link")
            fields.update(__EVENTTARGET=match[1], __EVENTARGUMENT=match[2])
            fields[self.page_field] = "100"
            response = request(self.session, "POST", self.url, data=fields)
            self.page_no += 1
