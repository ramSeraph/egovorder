#!/usr/bin/env bash
# Adapted from egazette/scripts/get_fromdate.sh for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

set -euo pipefail

source_name="${1:?Usage: get_fromdate.sh SOURCE [OUTPUT_FILE]}"
output_file="${2:-}"
export GOIR_SOURCE="$source_name"

prefix="$(python -c 'import os; from egovorder.srcs.datasrcs_info import get_prefix; print(get_prefix(os.environ["GOIR_SOURCE"], os.environ.get("TESTING") == "1"))')"
latest="$(ia search "identifier:${prefix}*" --sort 'date desc' -f date --parameters="page=1&rows=1" | python -c 'import json, sys; rows = [json.loads(line) for line in sys.stdin if line.strip()]; print(next((row["date"] for row in rows if row.get("date")), ""))')"
export GOIR_LATEST_DATE="$latest"
from_date="$(python - <<'PY'
import os
from datetime import datetime
from egovorder.srcs.datasrcs_info import get_start_date
latest = os.environ["GOIR_LATEST_DATE"]
date = datetime.fromisoformat(latest.replace("Z", "+00:00")) if latest else get_start_date(os.environ["GOIR_SOURCE"])
print(date.strftime("%d-%m-%Y"))
PY
)"
echo "$from_date"
if [[ -n "$output_file" ]]; then
  printf '%s\n' "$from_date" > "$output_file"
fi
