#!/usr/bin/env bash
# Shared local/CI entry point. Proxy lifecycle and data caching belong to CI.
set -euo pipefail

case "${SOURCE:-}" in
  andhra_goir|telangana_goir) ;;
  *) echo "Set SOURCE to andhra_goir or telangana_goir" >&2; exit 1 ;;
esac
case "${1:-}" in
  ''|--validate) ;;
  *) echo "Usage: SOURCE=... scripts/archive.sh [--validate]" >&2; exit 1 ;;
esac

: "${IA_ACCESS_KEY:?Set IA_ACCESS_KEY}"
: "${IA_SECRET_KEY:?Set IA_SECRET_KEY}"

from_date="${FROM_DATE:-}"
to_date="${TO_DATE:-$(date -u +'%d-%m-%Y')}"
last_n_days="${LAST_N_DAYS-180}"
run_time_mins="${RUN_TIME_MINS:-30}"
testing="${TESTING_MODE:-false}"
data_dir="${DATA_DIR:-data}"

case "$testing" in
  true) export TESTING=1 ;;
  false) unset TESTING ;;
  *) echo "TESTING_MODE must be true or false" >&2; exit 1 ;;
esac

from_date="$(FROM_DATE="$from_date" TO_DATE="$to_date" LAST_N_DAYS="$last_n_days" RUN_TIME_MINS="$run_time_mins" python - <<'PY'
import os
import sys
from datetime import datetime, timedelta

try:
    minutes = int(os.environ["RUN_TIME_MINS"])
    if not 1 <= minutes <= 300:
        raise ValueError("RUN_TIME_MINS must be between 1 and 300")
    end = datetime.strptime(os.environ["TO_DATE"], "%d-%m-%Y")
    start = os.environ["FROM_DATE"]
    days = os.environ["LAST_N_DAYS"]
    if start:
        start = datetime.strptime(start, "%d-%m-%Y")
    elif days:
        days = int(days)
        if days < 0:
            raise ValueError("LAST_N_DAYS must be nonnegative")
        start = end - timedelta(days=days)
    if start and start > end:
        raise ValueError("FROM_DATE must not be after TO_DATE")
except (ValueError, OverflowError) as exc:
    sys.exit(f"Invalid run settings: {exc}")
print(start.strftime("%d-%m-%Y") if start else "")
PY
)"

if [[ "${1:-}" == '--validate' ]]; then
  exit 0
fi

: "${PROXY_IP_ADDRESS:?Set PROXY_IP_ADDRESS to the source proxy host}"
proxy_auth="${PROXY_AUTH:-}"
proxy_url="http://${proxy_auth:+${proxy_auth}@}${PROXY_IP_ADDRESS}:8080"
# Convert with Python so leading zeroes do not trigger shell octal arithmetic.
run_time_secs="$(RUN_TIME_MINS="$run_time_mins" python -c 'import os; print(int(os.environ["RUN_TIME_MINS"]) * 60)')"

done_file=''
trap 'if [[ -n "$done_file" ]]; then rm -f "$done_file"; fi' EXIT

if [[ -z "$from_date" ]]; then
  from_date="$(bash scripts/get_fromdate.sh "$SOURCE")"
else
  done_file="$(mktemp)"
  bash scripts/get_donelist.sh "$SOURCE" "$from_date" "$to_date" "$done_file"
fi

sync_args=(-s "$SOURCE" -D "$data_dir" -t "$from_date" -T "$to_date" -W "$run_time_secs")
upload_args=(-u -D "$data_dir" -s "$SOURCE")
if [[ -n "$done_file" ]]; then
  sync_args+=(-L "$done_file")
fi
if [[ "$testing" == 'true' ]]; then
  upload_args+=(-S)
fi

echo "Archiving $SOURCE from $from_date through $to_date (download budget: $run_time_mins minutes)"
# Scope the proxy to the government portal requests. Archive queries/uploads
# use the normal connection, and the proxy credentials are never printed.
HTTP_PROXY="$proxy_url" HTTPS_PROXY="$proxy_url" \
  http_proxy="$proxy_url" https_proxy="$proxy_url" \
  python sync.py "${sync_args[@]}"

python iasync.py "${upload_args[@]}"

# Clear only this source, and only after successful uploads. Failed downloads
# or uploads leave files available for the workflow's cache and next run.
for folder in "$data_dir/raw/$SOURCE" "$data_dir/metatags/$SOURCE"; do
  if [[ -d "$folder" ]]; then
    find "$folder" -type f -delete
  fi
done
