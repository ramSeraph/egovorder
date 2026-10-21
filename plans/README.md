# Audited Andhra gaps

`andhra-2026-10.csv` contains 12,320 candidate orders across 1,217 dates from
2 June 2008 through 10 December 2025, identified in the October 2026 audit.
The audit compared Andhra's source listings through 12 December 2025 with
488,719 canonical Internet Archive items, whose latest order date was
12 December 2025. This is a fixed candidate list, not a claim that every listed
document remains downloadable or absent from IA today.

The four columns retain the matching key from the audit: order date, department,
order type and order number. The worker removes department code prefixes and
normalizes department punctuation for matching. It retrieves current source
rows through the existing Andhra parser and uses their document IDs to preserve
the original IA identifiers. Ambiguous matches or incomplete listings stop the
run; unavailable documents are recorded separately.

| Month | Candidates |
| --- | ---: |
| July 2008 | 1,762 |
| October 2017 | 1,365 |
| September 2018 | 1,038 |
| October 2018 | 1,974 |
| November 2018 | 1,833 |
| December 2018 | 1,032 |
| Other dates | 3,316 |

Explicitly unissued orders, rows already known to lack download links, and
possible matches under a different date were excluded from this plan. Current
source listings may reveal further unavailable orders.

The CSV SHA-256 is
`fbd0c889debd398681040e38661a4f028340de3be35cbecbbee8e23141c2aa30`.
Changing the plan gives the workflow a new cache namespace; the local worker
also rejects reuse of a state directory with a different plan.

Telangana was audited separately. Its four recoverable missing documents were
uploaded and verified; this plan contains Andhra orders only.
