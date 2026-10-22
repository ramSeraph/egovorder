# Adapted from egazette/iasync.py for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

"""Upload downloaded orders to Internet Archive."""

import argparse
import datetime
import logging
import os
from pathlib import Path
import sys

from .srcs.datasrcs_info import srcinfos


def timestamp(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD HH:MM:SS") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-D", "--data-dir", type=Path, required=True)
    parser.add_argument("-s", "--source", action="append", choices=tuple(srcinfos))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-u", "--upload", action="store_true")
    mode.add_argument("-m", "--update-meta", action="store_true")
    parser.add_argument("-M", "--update-meta-file", action="store_true")
    parser.add_argument("-a", "--access-key", default=os.environ.get("IA_ACCESS_KEY"))
    parser.add_argument("-k", "--secret-key", default=os.environ.get("IA_SECRET_KEY"))
    parser.add_argument("-S", "--sandbox", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("-r", "--relurl", action="append")
    selection.add_argument("-i", "--stdin", action="store_true")
    parser.add_argument("-t", "--from-time", type=timestamp)
    parser.add_argument("-T", "--to-time", type=timestamp)
    parser.add_argument("-d", "--days", type=int)
    parser.add_argument("-f", "--log-file", default="", help="filename under DATA_DIR/stats")
    parser.add_argument("-l", "--log-level", default="info",
                        choices=("critical", "error", "warning", "warn", "info", "debug"))
    args = parser.parse_args(argv)
    if not args.access_key or not args.secret_key:
        parser.error("set IA_ACCESS_KEY and IA_SECRET_KEY, or use -a and -k")
    if args.update_meta_file and not args.update_meta:
        parser.error("--update-meta-file requires --update-meta")
    if args.days is not None:
        if args.days < 0 or args.from_time or args.to_time:
            parser.error("--days must be nonnegative and cannot be combined with timestamps")
        args.to_time = datetime.datetime.now()
        args.from_time = args.to_time - datetime.timedelta(days=args.days)
    if args.from_time and args.to_time and args.from_time > args.to_time:
        parser.error("from-time must not be after to-time")
    names = args.source or list(srcinfos)
    relurls = args.relurl
    if args.stdin:
        relurls = [line.strip() for line in sys.stdin if line.strip()]
    if relurls is not None:
        for relurl in relurls:
            parts = relurl.split("/")
            if len(parts) < 3 or parts[0] not in names or any(
                part in ("", ".", "..") for part in parts
            ):
                parser.error(f"invalid order path or unselected source: {relurl}")

    # The scraper-only installation does not need archive or PDF dependencies.
    try:
        from .archive import OrderIA, Stats
    except ModuleNotFoundError as exc:
        parser.error(
            f"archive dependency missing ({exc.name}); run 'uv sync --locked --extra archive'"
        )
    from .utils.file_storage import FileManager

    statsdir = args.data_dir / "stats"
    statsdir.mkdir(parents=True, exist_ok=True)
    logfile = str(statsdir / args.log_file) if args.log_file else None
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s: %(name)s: %(levelname)s %(message)s",
        filename=logfile,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    storage = FileManager(str(args.data_dir), False, False)
    archive = OrderIA(storage, args.access_key, args.secret_key, args.log_level, logfile, args.sandbox)
    stats = Stats()
    if relurls is None:
        relurls = storage.find_matching_relurls(names, args.from_time, args.to_time)
    failed = False
    for relurl in relurls:
        source = archive.get_srcname(relurl)
        if args.upload:
            success = archive.upload(relurl)
            stats.update_upload(source, success)
        else:
            success = archive.update_meta(relurl, args.update_meta_file)
            stats.update_modify(source, success)
        failed = failed or not success
    print(stats.get_message(names))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
