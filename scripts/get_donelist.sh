#!/usr/bin/env bash
# Adapted from egazette/scripts/get_donelist.sh for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

set -euo pipefail

export GOIR_SOURCE="${1:?Usage: get_donelist.sh SOURCE FROM_DATE TO_DATE OUTPUT_FILE}"
export GOIR_FROM_DATE="${2:?Missing FROM_DATE (DD-MM-YYYY)}"
export GOIR_TO_DATE="${3:?Missing TO_DATE (DD-MM-YYYY)}"
output_file="${4:?Missing OUTPUT_FILE}"

prefix="$(python -c 'import os; from egovorder.srcs.datasrcs_info import get_prefix; print(get_prefix(os.environ["GOIR_SOURCE"], os.environ.get("TESTING") == "1"))')"
from_date="$(python -c 'import os; from datetime import datetime; print(datetime.strptime(os.environ["GOIR_FROM_DATE"], "%d-%m-%Y").strftime("%Y-%m-%d"))')"
to_date="$(python -c 'import os; from datetime import datetime; print(datetime.strptime(os.environ["GOIR_TO_DATE"], "%d-%m-%Y").strftime("%Y-%m-%d"))')"

strip_sandbox_prefix() {
  python -c 'import os, sys; from egovorder.srcs.datasrcs_info import TEST_PREFIX; prefix = TEST_PREFIX if os.environ.get("TESTING") == "1" else ""; [print(line.strip().removeprefix(prefix)) for line in sys.stdin if line.strip()]'
}

ia search "identifier:${prefix}* AND date:[$from_date TO $to_date]" -i | strip_sandbox_prefix > "$output_file"
ia search "identifier:${prefix}* AND NOT date:*" -i | strip_sandbox_prefix >> "$output_file"
