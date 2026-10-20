# egovorder

egovorder is a GOIR-focused version of
[egazette by Sushant Sinha](https://github.com/sushant354/egazette). It downloads
and archives government orders from the Andhra Pradesh and Telangana
Government Order Issue Registries (GOIR).

Only these sources are registered:

| Source | Portal | Archive collection | Earliest configured date |
| --- | --- | --- | --- |
| `andhra_goir` | https://goir.ap.gov.in/ | `andhra-goir` | 2008-06-01 |
| `telangana_goir` | https://goir.telangana.gov.in/ | `telangana-goir` | 2014-06-02 |

## Setup

Python 3.12 is used by CI. Install the system `libmagic` library if needed:
`brew install libmagic` on macOS, or `sudo apt-get install libmagic1` on Debian/Ubuntu.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/sync.txt
```

The scripts work directly from this checkout. To install the package and its
`egovorder-sync` / `egovorder-iasync` commands:

```sh
python -m pip install --no-deps .
```

## Download orders

```sh
# One source, one day
python sync.py -s andhra_goir -t 01-06-2025 -T 01-06-2025 -D data

# Both sources, a date range
python sync.py -t 01-06-2025 -T 07-06-2025 -D data

# Telangana, looking back seven days from today
python sync.py -s telangana_goir -d 7 -D data
```

Omitting `-s` selects both sources. Omitting all date options checks the last
15 days through today. `-a` starts at each source's configured earliest date.
Date ranges are inclusive. `python -m egovorder.sync` is equivalent to
`python sync.py`.

Files retain the original archive layout:

```text
data/raw/<source>/<YYYY-MM-DD>/<document-id>.pdf
data/metatags/<source>/<YYYY-MM-DD>/<document-id>.xml
```

Existing files are skipped. Use `-r` to replace raw files, `-m` to replace metadata,
and `-L donelist.txt` to skip known Internet Archive identifiers. `-W SECONDS`
requests a stop after that interval; workers finish the current day's work.
Standard proxy environment variables are supported; per-host overrides may be
set in `egovorder/utils/proxylist.py`.

Run `python sync.py --help` for all options.

## Internet Archive

Install the additional upload dependencies:

```sh
python -m pip install -r requirements/iasync.txt
```

Set `IA_ACCESS_KEY` and `IA_SECRET_KEY` in your environment, then run:

```sh
python iasync.py -u -D data -s andhra_goir
python iasync.py -u -D data -s telangana_goir
```

Use `-S` for the Internet Archive sandbox, or `-m` to update item metadata
(`-M` also replaces the metadata XML). `-r` selects a relative order path and
`-i` reads paths from standard input.

The existing identifiers are preserved:

- `in.gov.andhra.goir.<date>.<document-id>`
- `in.gov.telangana.goir.<date>.<document-id>`

Sandbox identifiers retain the inherited `egaztest.` prefix for compatibility.
The optional archive dependency includes PDF repair for documents rejected by
Internet Archive; Google OCR, email reporting, and unrelated cloud tools are
not included.

## GitHub Actions

Keep the code and workflows together on `main`, and make `main` the repository's
default branch. [GitHub runs scheduled workflows from the default branch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

| Workflow | When it runs | Purpose |
| --- | --- | --- |
| [Archive government orders](https://github.com/ramSeraph/egovorder/actions/workflows/archive.yml) | Daily at 08:30 IST, or manually | Recent orders, with a default 180-day lookback |
| [Catch up government orders](https://github.com/ramSeraph/egovorder/actions/workflows/catchup.yml) | Manually | Historical orders, uploading and saving progress after each day |
| [Keep scheduled workflows active](https://github.com/ramSeraph/egovorder/actions/workflows/keepalive.yml) | First of each month at 08:47 IST, or manually | Commit a timestamp to keep the repository active |
| [Offline tests](https://github.com/ramSeraph/egovorder/actions/workflows/tests.yml) | Pushes and pull requests | Fixture tests without portal access or uploads |

Both archive workflows use the same [shared job](.github/workflows/run-archive.yml),
check out the triggering commit once, and run the selected sources as independent
parallel jobs. They run only on `main`; no branch switching is needed.
Each archive job has a **four-hour maximum**, including setup and cleanup.

The keep-alive workflow updates `.github/keepalive` on `main` using the built-in
`GITHUB_TOKEN` with `contents: write`; it needs no extra secret. This provides
monthly repository activity before GitHub's
[60-day inactivity cutoff](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
If scheduled workflows have already been disabled, enable them in Actions first.

The existing operational settings from `egazette`'s `master` workflows are
preserved:

| Setting | Andhra Pradesh | Telangana |
| --- | --- | --- |
| Schedule | Daily at 03:00 UTC / 08:30 IST | Daily at 03:00 UTC / 08:30 IST |
| Lookback | 180 days | 180 days |
| Download budget | 30 minutes | 30 minutes |
| Proxy | Existing host, port 8080 | Temporary GCP proxy in `asia-south1-a`, port 8080 |

Configure these repository secrets before running it:

| Secret | Purpose |
| --- | --- |
| `IA_ACCESS_KEY`, `IA_SECRET_KEY` | Internet Archive uploads |
| `PROXY_IP_ADDRESS` | Existing Andhra proxy host |
| `PROXY_AUTH` | Proxy basic authentication, shared by both sources |
| `GCP_AUTH`, `GCP_PROJECT` | Telangana proxy provisioning; `GCP_AUTH` is the existing base64-encoded service-account JSON |

Telangana uses the same `gcp-simple-proxy-action` version (`v0.0.2`) to start
and stop its proxy. The stop step runs after a failed scrape or upload too.
Proxies apply to portal downloads; Internet Archive queries and uploads use
the normal connection.

For a manual run, choose **Actions → Archive government orders → Run workflow**
on `main`. Select `all`, `andhra_goir`, or `telangana_goir`. You can change the
lookback and download budget, or provide an explicit start/end date
(`DD-MM-YYYY`). An explicit start date takes precedence over the lookback.
Leaving both the start date and lookback empty resumes from the latest archived
date, falling back to the source's configured start date. `testing` selects the
Internet Archive sandbox.

Runs for the same source and destination are serialized. Unfinished downloads
are cached for the next run; only successfully archived source files are
cleared. Download time limits are cooperative and allow the current day's work
to finish; proxy setup and uploads are outside that download budget.

The daily workflow calls [scripts/archive.sh](scripts/archive.sh), which also runs
locally with the same settings once dependencies and credentials are available:

```sh
# Set IA_ACCESS_KEY, IA_SECRET_KEY, PROXY_IP_ADDRESS and PROXY_AUTH first.
SOURCE=andhra_goir bash scripts/archive.sh

# Validate inputs without querying portals or Internet Archive.
SOURCE=telangana_goir FROM_DATE=01-06-2025 TO_DATE=07-06-2025 \
  bash scripts/archive.sh --validate
```

Local runs use the supplied proxy host; GCP provisioning is handled by the
workflow. `DATA_DIR` defaults to `data`. Other overrides are `FROM_DATE`,
`TO_DATE`, `LAST_N_DAYS`, `RUN_TIME_MINS`, and `TESTING_MODE`.

### Historical catch-up

Open [Catch up government orders](https://github.com/ramSeraph/egovorder/actions/workflows/catchup.yml),
choose **Run workflow** on `main`, and select either source or `all`. It uses
the same six secrets and proxy setup as the daily workflow.

Leave `from_date` blank to start with the source's earliest configured date:
1 June 2008 for Andhra Pradesh or 2 June 2014 for Telangana. Set an explicit
`from_date` to backfill a narrower range. `to_date` defaults to today; both dates
use `DD-MM-YYYY`.

The default work budget is **210 minutes (3½ hours)**, with a **240-minute
(four-hour) job limit**. The work budget includes archive queries, downloads,
uploads, and retries; the remaining time allows for setup, proxy shutdown,
and cache saving. Manual work/download budgets must be between 1 and 210 minutes.

Catch-up uploads unfinished cached files first, then downloads and uploads one
day at a time. It advances its checkpoint only after that day's download and
upload commands succeed. Catch-up treats failed portal requests, unrecognized
result pages, and invalid downloads as errors so those days can be retried.
If the work budget expires, the unfinished day and its files are retained.

Run it again with the same source, start date, and sandbox setting to resume.
Checkpoints are stored under `data/catchup/`, separately for each source,
destination, and requested start date. Catch-up caches are separate from daily
caches; both workflows share a concurrency group so they do not archive the
same source to the same destination simultaneously. If a cache is unavailable,
catch-up starts at the requested start date and skips documents already present
in Internet Archive.

For a local catch-up run, set the same credentials and proxy environment
variables as for `archive.sh`, then run from this checkout:

```sh
SOURCE=andhra_goir FROM_DATE=01-06-2008 TO_DATE=30-06-2008 \
  RUN_TIME_MINS=210 python -m egovorder.catchup
```

`python -m egovorder.catchup --validate` checks settings without contacting
services. The offline test workflow needs no secrets.

## Tests

```sh
python -m pip install -r requirements/iasync.txt
python -m unittest discover -s tests -v
```

The tests cover both scrapers with local HTML fixtures, document storage,
source selection, archive identifiers and metadata, and mocked archive uploads.
They do not contact either government portal or upload documents.

## Origin and license

The original project is
[sushant354/egazette](https://github.com/sushant354/egazette).
This version extracts the GOIR implementation from the `workspace` branch of
the [ramSeraph/egazette fork](https://github.com/ramSeraph/egazette), at commit
`9938d8d9b3bbe2252f197c0543116d3a2cd32087`.

This repository contains the two GOIR scrapers and their shared download,
storage, and archive code. Gazette scrapers and unrelated tools were omitted.
The extraction starts a separate Git history.

This repository, including additions and modifications made for egovorder,
uses the same Apache License, Version 2.0 as egazette. The original copyright
notice for Sushant Sinha is retained in [LICENSE](LICENSE); the full license
terms are in [COPYING](COPYING).
