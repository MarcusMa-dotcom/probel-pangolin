#!/bin/sh
# Route a source to a destination through the REST API of Probel Pangolin.
#
#   ./set_crosspoint.sh 5 3          source 5 to destination 3 on level 1
#   ./set_crosspoint.sh 5 3 all      on all levels
#   BASE=http://10.0.0.5:8090 ./set_crosspoint.sh "CAM 5" "MON 3"
#
# The controller of Probel Pangolin must be connected to a router.
# Numbers go out as numbers, anything else as a name.

BASE="${BASE:-http://localhost:8090}"
LEVELS="${3:-1}"

value() {
  case "$1" in
    ""|*[!0-9]*) printf '"%s"' "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

if [ $# -lt 2 ]; then
  echo "usage: $0 SOURCE DESTINATION [LEVEL|all]" >&2
  exit 2
fi

curl -s -X POST "$BASE/api/v1/controller/route" \
  -H 'Content-Type: application/json' \
  -d "{\"source\": $(value "$1"), \"dest\": $(value "$2"), \"levels\": $(value "$LEVELS")}"
echo
