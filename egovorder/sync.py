"""Download government orders and their metadata to local storage."""

import argparse
import datetime
import logging
from pathlib import Path

from .srcs import datasrcs, datasrcs_info
from .utils import download
from .utils.file_storage import FileManager


def to_datetime(value):
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("expected a valid date in DD-MM-YYYY format")


def read_donefile(filename):
    return set(Path(filename).read_text().splitlines())


class DoneChecker:
    def __init__(self, doneset):
        self.doneset = doneset

    def is_done(self, relurl, metainfo):
        return bool(self.doneset) and datasrcs_info.get_identifier(
            relurl, metainfo, False
        ) in self.doneset


def execute(storage, srclist, agghosts, fromdate, todate, max_wait, all_dls, doneset):
    if fromdate is None and todate is not None:
        fromdate = todate
    elif fromdate is not None and todate is None:
        todate = datetime.datetime.today()

    sources = datasrcs.get_srcobjs(srclist, storage)
    for source in sources:
        source.set_done_checker(DoneChecker(doneset))
    return download.parallel_download(
        sources, agghosts, fromdate, todate, max_wait, all_dls
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Download Andhra Pradesh and Telangana GOIR orders. "
        "Both sources run by default; without dates, each checks the last 15 days."
    )
    parser.add_argument("-D", "--data-dir", required=True, type=Path)
    parser.add_argument("-s", "--source", action="append", choices=tuple(datasrcs.srcdict))
    parser.add_argument("-t", "--from-date", type=to_datetime)
    parser.add_argument("-T", "--to-date", type=to_datetime)
    parser.add_argument("-d", "--days", type=int, help="look back this many days from today")
    parser.add_argument("-a", "--all", action="store_true", help="download from each source's start date")
    parser.add_argument("-m", "--update-meta", action="store_true")
    parser.add_argument("-r", "--update-raw", action="store_true")
    parser.add_argument("-n", "--no-aggregation", action="store_true")
    parser.add_argument("-W", "--max-wait", type=int, help="seconds before requesting workers to stop")
    parser.add_argument("-L", "--done-file", type=Path, help="Internet Archive identifiers to skip")
    parser.add_argument("-f", "--log-file", help="filename under DATA_DIR/stats")
    parser.add_argument("-l", "--log-level", default="info",
                        choices=("critical", "error", "warning", "warn", "info", "debug"))
    args = parser.parse_args(argv)
    if args.days is not None:
        if args.days < 0:
            parser.error("--days must be nonnegative")
        if args.from_date or args.to_date:
            parser.error("--days cannot be combined with explicit dates")
        args.to_date = datetime.datetime.today()
        args.from_date = args.to_date - datetime.timedelta(days=args.days)
    if args.all and (args.from_date or args.to_date):
        parser.error("--all cannot be combined with date filters")
    if args.from_date and args.from_date > (args.to_date or datetime.datetime.today()):
        parser.error("from-date must not be after to-date")
    if args.max_wait is not None and args.max_wait <= 0:
        parser.error("--max-wait must be positive")

    doneset = read_donefile(args.done_file) if args.done_file else set()
    statsdir = args.data_dir / "stats"
    statsdir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s: %(name)s: %(levelname)s %(message)s",
        filename=statsdir / args.log_file if args.log_file else None,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    storage = FileManager(str(args.data_dir), args.update_meta, args.update_raw)
    success = execute(
        storage, args.source, not args.no_aggregation, args.from_date, args.to_date,
        args.max_wait, args.all, doneset,
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
