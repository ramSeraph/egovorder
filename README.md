# egovorder

egovorder is a version of
[egazette by Sushant Sinha](https://github.com/sushant354/egazette) for downloading
and archiving government orders from Andhra Pradesh, Telangana and Uttarakhand.

Only these sources are registered:

| Source | Portal | Archive collection | Earliest configured date |
| --- | --- | --- | --- |
| `andhra_goir` | https://goir.ap.gov.in/ | `andhra-goir` | 2008-06-01 |
| `telangana_goir` | https://goir.telangana.gov.in/ | `telangana-goir` | 2014-06-02 |
| `uttarakhand_gos` | https://go.uk.gov.in/ | `uttarakhand-gos` | 1999-05-13 |

## Quick start

Requires Python 3.10+ and the system `libmagic` library.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/iasync.txt

python sync.py -s andhra_goir -d 7 -D data
```

Set `IA_ACCESS_KEY` and `IA_SECRET_KEY` to upload downloaded orders:

```sh
python iasync.py -u -D data -s andhra_goir
```

Use `telangana_goir` for Telangana or `uttarakhand_gos` for Uttarakhand.
Both commands support `--help`.

## Workflows

- [Daily archive](https://github.com/ramSeraph/egovorder/actions/workflows/archive.yml)
- [Catch-up](https://github.com/ramSeraph/egovorder/actions/workflows/catchup.yml)
- [Andhra backfill](https://github.com/ramSeraph/egovorder/actions/workflows/backfill.yml)
- [Monthly keep-alive](https://github.com/ramSeraph/egovorder/actions/workflows/keepalive.yml)
- [Offline tests](https://github.com/ramSeraph/egovorder/actions/workflows/tests.yml)

## Origin and license

Extracted from the `workspace` branch of
[ramSeraph/egazette](https://github.com/ramSeraph/egazette) at commit
`9938d8d9b3bbe2252f197c0543116d3a2cd32087`, retaining its government-order scrapers.

This repository uses Apache License, Version 2.0. The original copyright
notice for Sushant Sinha is retained in [LICENSE](LICENSE); the full license
terms are in [COPYING](COPYING).
