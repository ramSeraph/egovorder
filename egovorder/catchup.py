"""Archive orders with bounded work and resumable progress."""

# Adapted from egazette's common-long action for egovorder.
# Modified to checkpoint each completed day and enforce a total work budget.
# See LICENSE and COPYING for the Apache 2.0 notice and terms.

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .srcs.datasrcs_info import get_start_date, srcinfos


ROOT = Path(__file__).resolve().parents[1]
STOP_GRACE_SECONDS = 5
RECENT_WORK_MINUTES = 210


class BudgetExpired(Exception):
    pass


@dataclass
class Settings:
    source: str
    start: date | None
    end: date
    minutes: int
    sandbox: bool
    data: Path
    env: dict

    @classmethod
    def from_env(cls, env):
        source = env.get("SOURCE", "")
        if source not in srcinfos:
            raise ValueError(f"Set SOURCE to one of: {', '.join(srcinfos)}")
        for key in ("IA_ACCESS_KEY", "IA_SECRET_KEY"):
            if not env.get(key):
                raise ValueError(f"Set {key}")
        testing = env.get("TESTING_MODE", "false")
        if testing not in ("true", "false"):
            raise ValueError("TESTING_MODE must be true or false")
        try:
            minutes = int(env.get("RUN_TIME_MINS") or "210")
        except ValueError:
            raise ValueError("RUN_TIME_MINS must be between 1 and 210") from None
        if not 1 <= minutes <= 210:
            raise ValueError("RUN_TIME_MINS must be between 1 and 210")
        try:
            if env.get("FROM_DATE"):
                start = datetime.strptime(env["FROM_DATE"], "%d-%m-%Y").date()
            else:
                start = None if source == "uttarakhand_gos" else get_start_date(source).date()
            end = (
                datetime.strptime(env["TO_DATE"], "%d-%m-%Y").date()
                if env.get("TO_DATE") else datetime.now(timezone.utc).date()
            )
        except ValueError:
            raise ValueError("Dates must be valid and use DD-MM-YYYY") from None
        if start is not None and start > end:
            raise ValueError("FROM_DATE must not be after TO_DATE")
        if end == date.max:
            raise ValueError("TO_DATE must be before 31-12-9999")
        data = Path(env.get("DATA_DIR") or "data")
        if not data.is_absolute():
            data = ROOT / data
        return cls(source, start, end, minutes, testing == "true", data, dict(env))

    @property
    def checkpoint(self):
        destination = "sandbox" if self.sandbox else "production"
        start = self.start.isoformat() if self.start is not None else 'latest'
        return self.data / "catchup" / f"{self.source}-{destination}-{start}.json"

    def next_date(self):
        if not self.checkpoint.exists():
            if self.start is None:
                raise ValueError("Latest archived date has not been initialized")
            return self.start
        try:
            next_date = date.fromisoformat(json.loads(self.checkpoint.read_text())["next_date"])
            if self.start is not None and next_date < self.start:
                raise ValueError("checkpoint precedes requested start")
            return next_date
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid catch-up checkpoint: {self.checkpoint}") from exc

    def save_progress(self, next_date):
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps({"next_date": next_date.isoformat()}) + "\n")
        temporary.replace(self.checkpoint)

    def pending_files(self):
        for folder in ("raw", "metatags"):
            for path in (self.data / folder / self.source).rglob("*"):
                if path.is_file():
                    yield path


def stop_process_group(process):
    """Stop the shell and its scraper/upload descendants before caching data."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The parent can exit before a child that ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(args, env, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BudgetExpired
    process = subprocess.Popen(args, cwd=ROOT, env=env, start_new_session=True)
    try:
        status = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        stop_process_group(process)
        raise BudgetExpired from None
    except BaseException:
        stop_process_group(process)
        raise
    if status:
        raise subprocess.CalledProcessError(status, args)


def catch_up(settings, runner=run_command, clock=time.monotonic):
    deadline = clock() + settings.minutes * 60
    env = dict(
        settings.env,
        SOURCE=settings.source,
        DATA_DIR=str(settings.data),
        TESTING_MODE="true" if settings.sandbox else "false",
        EGOVORDER_STRICT_DOWNLOADS="1",
    )
    if settings.start is None and not settings.checkpoint.exists():
        # Seed once from IA, then retain a stable checkpoint even if another
        # uploader later archives a newer date while this catch-up is unfinished.
        latest_file = settings.checkpoint.with_suffix('.from-date')
        latest_file.parent.mkdir(parents=True, exist_ok=True)
        query_env = dict(env)
        if settings.sandbox:
            query_env['TESTING'] = '1'
        else:
            query_env.pop('TESTING', None)
        runner(['bash', 'scripts/get_fromdate.sh', settings.source, str(latest_file)], query_env, deadline)
        latest = datetime.strptime(latest_file.read_text().strip(), '%d-%m-%Y').date()
        settings.save_progress(latest)
        latest_file.unlink()
    day = settings.next_date()

    # Recover cached uploads before contacting the portal. Do not advance the
    # checkpoint: the interrupted day may still have missing downloads.
    if any(settings.pending_files()):
        args = ["python", "iasync.py", "-u", "-D", str(settings.data), "-s", settings.source]
        if settings.sandbox:
            args.append("-S")
        print("Uploading unfinished downloads before resuming catch-up", flush=True)
        runner(args, env, deadline)
        for path in settings.pending_files():
            path.unlink()

    while day <= settings.end:
        remaining = deadline - clock()
        if remaining <= 0:
            raise BudgetExpired
        formatted = day.strftime("%d-%m-%Y")
        day_env = dict(
            env,
            FROM_DATE=formatted,
            TO_DATE=formatted,
            LAST_N_DAYS="",
            RUN_TIME_MINS=str(min(settings.minutes, max(1, math.ceil(remaining / 60)))),
        )
        print(f"Catch-up: {settings.source} on {formatted}", flush=True)
        # A single day cannot be partially skipped by sync.py's cooperative
        # timer. The outer deadline also bounds queries, uploads and retries.
        runner(["bash", "scripts/archive.sh"], day_env, deadline)
        day += timedelta(days=1)
        settings.save_progress(day)
    print(f"Catch-up complete through {settings.end.isoformat()}", flush=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", help="validate settings without contacting services")
    parser.add_argument("--recent", action="store_true", help="bound the recent archive's downloads and uploads to 210 minutes")
    args = parser.parse_args(argv)
    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        settings = Settings.from_env(os.environ)
        if args.validate:
            return 0
        if not settings.env.get("PROXY_IP_ADDRESS"):
            raise ValueError("Set PROXY_IP_ADDRESS to the source proxy host")
        if args.recent:
            run_command(["bash", "scripts/archive.sh"], settings.env,
                        time.monotonic() + RECENT_WORK_MINUTES * 60)
        else:
            catch_up(settings)
    except BudgetExpired:
        print("Archive work budget reached; progress and unfinished files are retained for the next run.", flush=True)
    except KeyboardInterrupt:
        print("Catch-up interrupted; progress and unfinished files are retained.", file=sys.stderr)
        return 130
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Catch-up failed: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
