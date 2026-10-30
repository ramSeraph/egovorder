# egovorder

egovorder is a version of
[egazette by Sushant Sinha](https://github.com/sushant354/egazette) for downloading
and archiving government orders from Andhra Pradesh, Telangana and Uttarakhand.

Only these sources are registered:

| Source | Portal | Internet Archive collection | Earliest configured date |
| --- | --- | --- | --- |
| `andhra_goir` | https://goir.ap.gov.in/ | [andhra-goir](https://archive.org/details/andhra-goir) | 2008-06-01 |
| `telangana_goir` | https://goir.telangana.gov.in/ | [telangana-goir](https://archive.org/details/telangana-goir) | 2014-06-02 |
| `uttarakhand_gos` | https://go.uk.gov.in/ | [uttarakhand-gos](https://archive.org/details/uttarakhand-gos) | 1999-05-13 |

## Quick start

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/) and the system `libmagic`
library.

```sh
uv sync --locked --extra archive

uv run --locked --extra archive python sync.py -s andhra_goir -d 7 -D data
```

Set `IA_ACCESS_KEY` and `IA_SECRET_KEY` to upload downloaded orders:

```sh
uv run --locked --extra archive python iasync.py -u -D data -s andhra_goir
```

Use `telangana_goir` for Telangana or `uttarakhand_gos` for Uttarakhand.
Both commands support `--help`.

## Workflows

| State | Daily archive (08:30 IST) | Catch-up |
| --- | --- | --- |
| Andhra Pradesh | [Daily](https://github.com/ramSeraph/egovorder/actions/workflows/andhra-daily.yml) | [Disabled](https://github.com/ramSeraph/egovorder/actions/workflows/andhra-catchup.yml) |
| Telangana | [Daily](https://github.com/ramSeraph/egovorder/actions/workflows/telangana-daily.yml) | Retired |
| Uttarakhand | [Daily](https://github.com/ramSeraph/egovorder/actions/workflows/uttarakhand-daily.yml) | Retired |

- [Andhra backfill](https://github.com/ramSeraph/egovorder/actions/workflows/backfill.yml): historical audit complete; disabled
- [Monthly keep-alive](https://github.com/ramSeraph/egovorder/actions/workflows/keepalive.yml)

## Origin and license

Extracted from the `workspace` branch of
[ramSeraph/egazette](https://github.com/ramSeraph/egazette) at commit
`9938d8d9b3bbe2252f197c0543116d3a2cd32087`, retaining its government-order scrapers.

This repository uses Apache License, Version 2.0. The original copyright
notice for Sushant Sinha is retained in [LICENSE](LICENSE); the full license
terms are in [COPYING](COPYING).
