# egovorder

Download and archive government orders from the Andhra Pradesh and Telangana
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

The two archiver workflows are manually dispatched, as in the source branch.
They run the selected checkout and accept optional dates or a day count.
With no start date, they resume from the latest date in Internet Archive,
falling back to the configured source start date.

Before running an archiver workflow on GitHub, configure repository secrets
`IA_ACCESS_KEY` and `IA_SECRET_KEY`. The `testing` input selects the sandbox.
Unfinished downloads are cached for the next run; successfully archived files
are cleared. No scraping schedule is configured.

The separate test workflow runs offline checks on pushes and pull requests.

## Tests

```sh
python -m pip install -r requirements/iasync.txt
python -m unittest discover -s tests -v
```

The tests cover both scrapers with local HTML fixtures, document storage,
source selection, archive identifiers and metadata, and mocked archive uploads.
They do not contact either government portal or upload documents.

## Origin and license

Extracted from the local `workspace` branch of `ramSeraph/egazette` at commit
`9938d8d9b3bbe2252f197c0543116d3a2cd32087`.

This repository contains the two GOIR scrapers and their shared download,
storage, and archive code. Gazette scrapers and unrelated tools were omitted.
The extraction starts a separate Git history; the original repository and its
local files are untouched.

The inherited Apache 2.0 license and attribution are retained in
[LICENSE](LICENSE) and [COPYING](COPYING).
