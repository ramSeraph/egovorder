"""Resume the audited Andhra gaps, retaining a per-record outcome and upload receipt."""
import argparse
from collections import Counter, defaultdict
import configparser
import csv
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import time
import warnings
from urllib3.exceptions import InsecureRequestWarning
from urllib.parse import urljoin
import zipfile

from internetarchive import get_session, upload
import pymupdf
import requests

from .backfill_portal import ANDHRA_ORIGIN, AndhraPortal, fingerprint, key, request, retryable
from egovorder.srcs.datasrcs_info import TEST_PREFIX, get_identifier, srcinfos
from egovorder.utils.file_storage import FileManager
from egovorder.utils.metainfo import MetaInfo

ROOT = Path("data/backfill")
SOURCE = "andhra_goir"
PLAN = Path(__file__).resolve().parent.parent / "plans/andhra-2026-10.csv"
FINAL_STATUSES = {"uploaded", "already_archived"}
GAP_STATUSES = {"unavailable", "not_found", "no_download_link"}
MONTHS = ("2008-07", "2017-10", "2018-09", "2018-10", "2018-11", "2018-12")
PARTITIONS = (*MONTHS, "other", "latest")
AUDIT_END = date(2025, 12, 12)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def check_space(root):
    if shutil.disk_usage(root).free < 5 * 2**30:
        raise RuntimeError("Less than 5 GiB free; all pending files retained")


def document_format(data):
    """Accept validated PDF/Word files; classify HTML and empty bodies as gaps."""
    if data.lstrip().startswith(b"%PDF"):
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if len(doc) > 0:
                    return "pdf"
        except Exception:
            return None
    if zipfile.is_zipfile(BytesIO(data)):
        try:
            with zipfile.ZipFile(BytesIO(data)) as doc:
                if "word/document.xml" in doc.namelist() and doc.testzip() is None:
                    return "docx"
        except (zipfile.BadZipFile, RuntimeError):
            return None
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        from egovorder.utils.ext_ops import get_buffer_type
        if get_buffer_type(data) == "application/msword":
            return "doc"
    return None


class Backfill:
    def __init__(self, root, *, insecure_source=False, testing=False, proxy=None):
        self.root = Path(root).resolve()
        self.insecure_source = insecure_source
        self.testing = testing
        self.proxy = proxy
        self.root.mkdir(parents=True, exist_ok=True)
        check_space(self.root)
        self.storage = FileManager(str(self.root / "data"), False, False)
        config = configparser.ConfigParser()
        config.read(Path.home() / ".config/internetarchive/ia.ini")
        self.access = os.environ.get("IA_ACCESS_KEY") or config.get("s3", "access", fallback="")
        self.secret = os.environ.get("IA_SECRET_KEY") or config.get("s3", "secret", fallback="")
        if not self.access or not self.secret:
            raise ValueError("Set IA_ACCESS_KEY and IA_SECRET_KEY, or configure ~/.config/internetarchive/ia.ini")
        self.ia_session = get_session({"general": {"secure": True}, "s3": {"access": self.access, "secret": self.secret}})
        self.ia_session.verify = True
        self.public = requests.Session()
        self.public.trust_env = False
        self.ia_session.trust_env = False
        self.ia_session.proxies.clear()
        self.counts = Counter(json.loads(p.read_text())["status"] for p in (self.root / "records").glob("*.json"))
        self.current_day = None
        self.started_at = now()

    def record_path(self, row):
        return self.root / "records" / (fingerprint(key(row)) + ".json")

    def outcome(self, row):
        path = self.record_path(row)
        return json.loads(path.read_text()) if path.exists() else None

    def save(self, row, status, **details):
        result = {"candidate_key": key(row), "date": row["date"], "status": status,
                  "updated_at": now(), **details}
        previous = self.outcome(row)
        atomic_json(self.record_path(row), result)
        if previous:
            self.counts[previous["status"]] -= 1
        self.counts[status] += 1
        if status in GAP_STATUSES:
            print(f"{status.upper()} {details.get('identifier') or key(row)}", flush=True)
        return result

    def progress(self, status):
        completed = list((self.root / "days").glob("*.json"))
        result = {
            "pid": os.getpid(), "status": status, "started_at": self.started_at,
            "updated_at": now(), "current_day": self.current_day,
            "time_limit_minutes": getattr(self, "time_limit_minutes", None),
            "verify_source_tls": not self.insecure_source, "sandbox": self.testing,
            "completed_days": len(completed), "outcomes": {k: v for k, v in self.counts.items() if v},
            "free_gib": round(shutil.disk_usage(self.root).free / 2**30, 2),
        }
        atomic_json(self.root / "progress.json", result)
        return result

    def remote_item(self, identifier):
        response = request(self.public, "GET", f"https://archive.org/metadata/{identifier}")
        data = response.json()
        if not isinstance(data, dict) or "error" in data:
            raise RuntimeError(f"Invalid IA metadata response for {identifier}")
        return data

    def finish_upload(self, row, pending):
        for attempt in range(4):
            try:
                return self._finish_upload(row, pending)
            except requests.RequestException as exc:
                if attempt == 3 or not retryable(exc):
                    raise
                print(f"Retrying upload after {type(exc).__name__}; checking accepted files first", flush=True)
                time.sleep(5 * (attempt + 1))

    def file_path(self, file):
        path = (self.root / file["path"]).resolve()
        if not path.is_relative_to(self.root) or Path(file["path"]).is_absolute():
            raise ValueError("Invalid pending file path")
        return path

    def _finish_upload(self, row, pending):
        identifier = pending["identifier"]
        files = pending["files"]
        remote = self.remote_item(identifier)
        existing = {f["name"]: f for f in remote.get("files", [])}
        needed = []
        for file in files:
            path = self.file_path(file)
            if path.is_file() and hashlib.md5(path.read_bytes()).hexdigest() != file["md5"]:
                raise RuntimeError(f"Local file changed: {path}")
            archived = existing.get(file["name"])
            if archived:
                if archived.get("md5") != file["md5"]:
                    raise RuntimeError(f"Existing IA file differs: {identifier}/{file['name']}")
            elif not path.is_file():
                raise RuntimeError(f"Pending upload file missing: {path}")
            else:
                needed.append(str(path))
        if needed:
            print(f"UPLOAD {identifier} ({sum(Path(p).stat().st_size for p in needed)} bytes)", flush=True)
            responses = upload(
                identifier, needed, metadata=None if remote.get("metadata") else pending["metadata"],
                access_key=self.access, secret_key=self.secret, archive_session=self.ia_session,
                verify=True, retries=2, retries_sleep=5, request_kwargs={"timeout": (15, 90)},
            )
            if len(responses) != len(needed):
                raise RuntimeError(f"Incomplete upload response for {identifier}")
            for response in responses:
                response.raise_for_status()
        result = self.save(row, "uploaded", identifier=identifier, files=files,
                           metadata=pending["metadata"], uploaded_at=now())
        # Each request was accepted with Content-MD5. Public IA ingestion can lag.
        for file in files:
            self.file_path(file).unlink(missing_ok=True)
        print(f"UPLOADED {identifier}", flush=True)
        self.progress("running")
        return result

    def process(self, row, portal, retry_unavailable=False):
        previous = self.outcome(row)
        if previous and previous["status"] in FINAL_STATUSES:
            return previous
        if previous and previous["status"] == "downloaded":
            return self.finish_upload(row, previous)
        if previous and previous["status"] in GAP_STATUSES and not retry_unavailable:
            return previous
        check_space(self.root)
        identifier = row.get("identifier")
        if identifier and self.testing:
            identifier = TEST_PREFIX + identifier
        if not identifier or not row.get("href"):
            return self.save(row, "no_download_link", source_row=row)
        remote = self.remote_item(identifier)
        docid = identifier.rsplit(".", 1)[-1]
        files = {f["name"]: f for f in remote.get("files", []) if f.get("md5") and int(f.get("size", 0)) > 0}
        if remote.get("metadata") and f"{docid}.xml" in files and any(
            f"{docid}.{ext}" in files for ext in ("pdf", "doc", "docx")
        ):
            return self.save(row, "already_archived", identifier=identifier)
        gid, kind, *_ = re.findall(r'"([^"]+)"', row["href"])
        if not gid.isdigit() or not re.fullmatch(r"[A-Za-z]+", kind):
            raise RuntimeError("Unrecognized Andhra document identifier")
        url = urljoin(portal.url, "dgo.ashx")
        response = request(portal.session, "GET", url, params={"gid": gid, "fileType": kind}, missing_ok=True)
        if response.status_code in (404, 410):
            return self.save(row, "unavailable", identifier=identifier, reason="http_missing",
                             http_status=response.status_code, source_row=row)
        response.raise_for_status()
        extension = document_format(response.content)
        if not extension:
            return self.save(row, "unavailable", identifier=identifier, reason="empty_or_invalid_document",
                             bytes=len(response.content), source_row=row)
        info = MetaInfo()
        for field, value in row.items():
            if field not in {"identifier", "serial", "href", "status", "date"}:
                info.set_field(field, value)
        info.set_date(date.fromisoformat(row["date"]))
        info.set_url(response.url)
        relurl = f"{SOURCE}/{row['date']}/{kind}-{gid}"
        if get_identifier(relurl, info, self.testing) != identifier:
            raise RuntimeError("Identifier mismatch")
        rawfile = Path(self.storage.rawdir) / f"{relurl}.{extension}"
        rawfile.parent.mkdir(parents=True, exist_ok=True)
        rawfile.write_bytes(response.content)
        self.storage.save_metainfo(SOURCE, relurl, info)
        metafile = Path(self.storage.get_metafile_path(relurl))
        files = [
            {"path": str(path.relative_to(self.root)), "name": path.name, "bytes": path.stat().st_size,
             "md5": hashlib.md5(path.read_bytes()).hexdigest()}
            for path in (rawfile, metafile)
        ]
        pending = self.save(
            row, "downloaded", identifier=identifier, files=files,
            metadata=info.get_ia_metadata(srcinfos[SOURCE], self.testing),
        )
        return self.finish_upload(row, pending)

    def day(self, day, candidates, retry_unavailable=False, portal=None):
        self.current_day = day
        self.progress("running")
        active, outcomes = {}, []
        # Finish cached uploads before making any source requests.
        for candidate_key, candidate in candidates.items():
            previous = self.outcome(candidate)
            if previous and previous["status"] == "downloaded":
                outcomes.append(self.finish_upload(candidate, previous))
            elif previous and (previous["status"] in FINAL_STATUSES or (
                previous["status"] in GAP_STATUSES and not retry_unavailable
            )):
                outcomes.append(previous)
            else:
                active[candidate_key] = candidate
        if active:
            print(f"DAY {day}: querying {len(active)} audited candidates", flush=True)
            portal = portal or AndhraPortal(date.fromisoformat(day), insecure_source=self.insecure_source, proxy=self.proxy)
            try:
                matched = defaultdict(list)
                for row in portal.listed_rows():
                    if key(row) in active:
                        matched[key(row)].append(row)
                if any(len(rows) > 1 for rows in matched.values()):
                    raise RuntimeError(f"Ambiguous source rows on {day}; stopping for review")
                for candidate_key, candidate in active.items():
                    rows = matched.get(candidate_key, [])
                    if not rows:
                        outcomes.append(self.save(candidate, "not_found", source_row=candidate))
                    else:
                        outcomes.append(self.process(rows[0], portal, retry_unavailable))
            finally:
                portal.session.close()
        counts = dict(Counter(row["status"] for row in outcomes))
        atomic_json(self.root / "days" / f"{day}.json", {
            "date": day, "completed_at": now(), "outcomes": counts,
        })
        print(f"DAY {day}: {json.dumps(counts)}", flush=True)
        self.progress("running")


class BudgetExpired(BaseException):
    # Document parsers and HTTP libraries must not swallow the total-work timer.
    pass


def deadline(signum, frame):
    raise BudgetExpired


def interrupted(signum, frame):
    raise KeyboardInterrupt


def load_plan(path, start=None, end=None, partition="all"):
    if partition not in ("all", "other", *MONTHS):
        raise ValueError("Unknown audit partition")
    grouped, seen = defaultdict(dict), set()
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            day = date.fromisoformat(row["date"])
            if not date(2008, 6, 1) <= day <= date(2025, 12, 12):
                raise ValueError("Plan contains a date outside the Andhra audit")
            if not all(row.get(field, "").strip() for field in ("department", "gotype", "gonum")):
                raise ValueError("Incomplete audit candidate")
            candidate_key = key(row)
            if candidate_key in seen:
                raise ValueError("Duplicate audit candidate")
            seen.add(candidate_key)
            month = row["date"][:7]
            selected = partition == "all" or month == partition or (partition == "other" and month not in MONTHS)
            if selected and (start is None or start <= day) and (end is None or day <= end):
                grouped[row["date"]][candidate_key] = row
    if not grouped:
        raise ValueError("No audited candidates in the selected date range")
    return grouped


def run_plan(runner, grouped, retry_unavailable=False):
    # Recover the largest holes first; each date has its own checkpoint.
    for day in sorted(grouped, key=lambda d: (-len(grouped[d]), d)):
        checkpoint = runner.root / "days" / f"{day}.json"
        if checkpoint.exists():
            counts = json.loads(checkpoint.read_text())["outcomes"]
            if not retry_unavailable or not any(counts.get(s) for s in GAP_STATUSES):
                continue
        runner.day(day, grouped[day], retry_unavailable)


def latest_ia_date(session):
    response = request(session, "GET", "https://archive.org/advancedsearch.php", params={
        "q": f"identifier:{srcinfos[SOURCE]['prefix']}* AND date:[* TO *]",
        "fl[]": ["date", "identifier"], "sort[]": "date desc", "rows": 1, "output": "json",
    })
    docs = response.json()["response"]["docs"]
    if not docs or not docs[0].get("date"):
        raise RuntimeError("Could not determine the latest archived Andhra order date")
    return date.fromisoformat(docs[0]["date"][:10])


def latest_candidates(root):
    grouped = {}
    for path in sorted((root / "candidates").glob("*.json")):
        grouped[path.stem] = {key(row): row for row in json.loads(path.read_text())}
    return grouped


def catch_up_latest(runner, retry_unavailable=False, today=None):
    today = today or datetime.now(timezone.utc).date()
    checkpoint = runner.root / "latest.json"
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text())
        start = date.fromisoformat(state["start_date"])
        cursor = date.fromisoformat(state["next_date"])
    else:
        # Always seed from production, including sandbox tests of this same job.
        start = cursor = latest_ia_date(runner.public)
    if start < AUDIT_END or cursor < start or start > today or cursor > today + timedelta(days=1):
        raise ValueError("Invalid latest-date checkpoint; catch-up must not overlap the historical audit")
    atomic_json(checkpoint, {"start_date": start, "next_date": cursor, "through": today})

    def archive_day(day):
        day = day.isoformat()
        path = runner.root / "candidates" / f"{day}.json"
        if path.exists():
            candidates = {key(row): row for row in json.loads(path.read_text())}
            runner.day(day, candidates, retry_unavailable)
        else:
            portal = AndhraPortal(date.fromisoformat(day), insecure_source=runner.insecure_source, proxy=runner.proxy)
            try:
                rows = portal.listed_rows()
                candidates = {key(row): row for row in rows}
                if len(candidates) != len(rows):
                    raise RuntimeError(f"Ambiguous source rows on {day}; stopping for review")
                atomic_json(path, rows)
                runner.day(day, candidates, retry_unavailable, portal=portal)
            finally:
                portal.session.close()

    if retry_unavailable:
        for path in sorted((runner.root / "days").glob("*.json")):
            if path.stem < cursor.isoformat():
                counts = json.loads(path.read_text())["outcomes"]
                if any(counts.get(status) for status in GAP_STATUSES):
                    archive_day(date.fromisoformat(path.stem))
    while cursor <= today:
        archive_day(cursor)
        cursor += timedelta(days=1)
        atomic_json(checkpoint, {"start_date": start, "next_date": cursor, "through": today})


def write_report(runner, grouped, status):
    counts = Counter()
    with (runner.root / "outcomes.csv").open("w", newline="") as stream:
        fields = ["date", "department", "gotype", "gonum", "status", "identifier", "reason"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for day in sorted(grouped):
            for row in grouped[day].values():
                outcome = runner.outcome(row) or {"status": "not_attempted"}
                counts[outcome["status"]] += 1
                writer.writerow({field: outcome.get(field, row.get(field, "")) for field in fields})
    if status == "complete" and any(counts.get(s) for s in GAP_STATUSES):
        status = "complete_with_source_gaps"
    progress = runner.progress(status)
    progress["selection"] = {
        "from_date": min(grouped, default=None), "to_date": max(grouped, default=None),
        "dates": len(grouped), "candidates": sum(counts.values()), "outcomes": dict(counts),
    }
    checkpoint = runner.root / "latest.json"
    if checkpoint.exists():
        progress["catchup"] = json.loads(checkpoint.read_text())
    atomic_json(runner.root / "progress.json", progress)
    summary = [
        "### Andhra backfill", "",
        f"Status: **{status}**. Destination: {'sandbox' if runner.testing else 'production'}.", "",
        f"Selected {sum(counts.values()):,} candidates across {len(grouped):,} dates "
        f"({min(grouped, default='not listed yet')} through {max(grouped, default='not listed yet')}).", "",
        "| Outcome | Orders |", "| --- | ---: |",
        *(f"| {name} | {count:,} |" for name, count in sorted(counts.items())), "",
        "Unavailable files and missing source records remain gaps; they are not archived.",
        "Run again to resume, or enable retry_unavailable to recheck source gaps.", "",
    ]
    text = "\n".join(summary)
    (runner.root / "summary.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
            stream.write(text)
    print(json.dumps(progress), flush=True)


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%m-%Y").date()
    except ValueError:
        raise argparse.ArgumentTypeError("Dates must use DD-MM-YYYY") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--upload", action="store_true", help="Download and upload; default only validates the plan")
    mode.add_argument("--validate", action="store_true", help="Validate the plan without contacting services")
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--state-dir", type=Path, default=ROOT)
    parser.add_argument("--partition", choices=("all", *PARTITIONS), default="all",
                        help="One independent audit range, or latest for catch-up from IA through today")
    parser.add_argument("--from-date", type=parse_date, default=os.environ.get("FROM_DATE", ""))
    parser.add_argument("--to-date", type=parse_date, default=os.environ.get("TO_DATE", ""))
    parser.add_argument("--minutes", type=int, default=os.environ.get("RUN_TIME_MINS") or None,
                        help="Total work budget; local runs have no limit by default")
    parser.add_argument("--testing", action="store_true", help="Use Internet Archive sandbox and separate progress")
    parser.add_argument("--retry-unavailable", action="store_true")
    parser.add_argument("--insecure-source", action="store_true",
                        help="Ignore certificate errors only for https://goir.ap.gov.in/")
    args = parser.parse_args(argv)
    if args.minutes is not None and args.minutes < 1:
        parser.error("--minutes must be positive")
    if os.environ.get("GITHUB_ACTIONS") == "true" and (args.minutes is None or args.minutes > 210):
        parser.error("GitHub Actions requires a work budget between 1 and 210 minutes")
    if args.from_date and args.to_date and args.from_date > args.to_date:
        parser.error("Start date is later than end date")
    try:
        if args.partition == "latest" and (args.from_date or args.to_date):
            raise ValueError("latest determines its dates from Internet Archive and today")
        grouped = {} if args.partition == "latest" else load_plan(args.plan, args.from_date, args.to_date, args.partition)
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    if args.partition == "latest":
        print("Catch-up: latest archived Andhra order date through today; resume saved progress if present", flush=True)
    else:
        print(f"Plan: {sum(map(len, grouped.values()))} candidates across {len(grouped)} dates", flush=True)
    if not args.upload:
        return 0
    root = args.state_dir / args.partition / ("sandbox" if args.testing else "production")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another backfill is using this state directory")
        if args.partition != "latest":
            plan_hash = hashlib.sha256(args.plan.read_bytes()).hexdigest()
            plan_state = root / "plan.json"
            if plan_state.exists() and json.loads(plan_state.read_text())["sha256"] != plan_hash:
                parser.error("Audit plan changed; use a separate state directory or review existing progress")
            atomic_json(plan_state, {"source": args.plan.name, "sha256": plan_hash})
        proxy_host = os.environ.get("PROXY_IP_ADDRESS", "")
        proxy_auth = os.environ.get("PROXY_AUTH", "")
        proxy = f"http://{proxy_auth + '@' if proxy_auth else ''}{proxy_host}:8080" if proxy_host else None
        if args.insecure_source:
            print(f"Certificate verification disabled for {ANDHRA_ORIGIN}", flush=True)
            warnings.filterwarnings("ignore", category=InsecureRequestWarning,
                                    message=r"Unverified HTTPS request is being made to host 'goir\.ap\.gov\.in'\.")
        runner = Backfill(root, insecure_source=args.insecure_source, testing=args.testing, proxy=proxy)
        runner.time_limit_minutes = args.minutes
        prior_alarm = signal.signal(signal.SIGALRM, deadline)
        prior_term = signal.signal(signal.SIGTERM, interrupted)
        signal.alarm(0 if args.minutes is None else args.minutes * 60)
        status, result = "complete", 0
        try:
            if args.partition == "latest":
                catch_up_latest(runner, args.retry_unavailable)
            else:
                run_plan(runner, grouped, args.retry_unavailable)
        except BudgetExpired:
            status = "paused_at_time_limit"
        except KeyboardInterrupt:
            status, result = "interrupted", 130
        except Exception as exc:
            status, result = "stopped_on_error", 1
            message = str(exc)
            for secret in (runner.access, runner.secret, proxy_auth, proxy):
                if secret:
                    message = message.replace(secret, "[redacted]")
            print(f"STOPPED: {type(exc).__name__}: {message}", flush=True)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prior_alarm)
            signal.signal(signal.SIGTERM, prior_term)
            if args.partition == "latest":
                grouped = latest_candidates(runner.root)
            write_report(runner, grouped, status)
        return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
