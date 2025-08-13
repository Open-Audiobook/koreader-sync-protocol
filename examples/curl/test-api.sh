#!/usr/bin/env bash
#
# test-api.sh
# cURL-based interaction examples for KOReader Sync Protocol.
#
# Provides convenience functions:
#   ko_auth         - test authentication
#   ko_put <file> <page> <total_pages> [id_mode]
#   ko_get <file> [id_mode]
#
# Environment:
#   KOREADER_SYNC_USER   (required)
#   KOREADER_SYNC_PASS   (required; plaintext, will be md5sum'ed)
#   KOREADER_ID_MODE     (optional: filename | partial, default=filename)
#
# Dependencies: bash, coreutils (md5sum), dd (for partial MD5), hexdump (optional)
#
# NOTE: Partial MD5 sampling replicates KOReader algorithm.
#
set -euo pipefail

BASE_URL="https://sync.koreader.rocks"
ACCEPT="application/vnd.koreader.v1+json"

require_env() {
  if [[ -z "${KOREADER_SYNC_USER:-}" || -z "${KOREADER_SYNC_PASS:-}" ]]; then
    echo "Set KOREADER_SYNC_USER and KOREADER_SYNC_PASS env vars." >&2
    exit 1
  fi
}

md5_string() {
  # Portable-ish: Linux md5sum; macOS users may alias md5sum='md5 -r'
  printf "%s" "$1" | md5sum | awk '{print $1}'
}

doc_id_filename() {
  local path="$1"
  local base
  base="$(basename "$path")"
  md5_string "$base"
}

doc_id_partial() {
  local path="$1"
  local step=1024
  local size=1024
  local tmpfile
  tmpfile="$(mktemp)"
  # We'll build a concatenation of each sampled block into a temp file then md5sum once.
  # Offsets: 1024 << (2*i) for i=-1..10
  for i in {-1..10}; do
    # offset = step << (2*i)
    # bash doesn't do negative exponent shift; compute via bc
    local shift=$(( 2 * i ))
    local offset
    if (( shift < 0 )); then
      # 1024 << -2 == 1024 >> 2 => 256 etc.
      local pos_shift=$(( -shift ))
      offset=$(( step >> pos_shift ))
    else
      offset=$(( step << shift ))
    fi
    # If offset beyond file size, break
    if (( offset < 0 )); then
      continue
    fi
    # Attempt read
    if ! dd if="$path" bs=1 skip="$offset" count="$size" 2>/dev/null >> "$tmpfile"; then
      break
    fi
    # If we got fewer bytes than requested, check file end
    local got
    got=$(stat -c '%s' "$tmpfile" 2>/dev/null || stat -f '%z' "$tmpfile")
    # (Simplified: we'll just continue; dd stops if EOF.)
  done
  md5sum "$tmpfile" | awk '{print $1}'
  rm -f "$tmpfile"
}

doc_id() {
  local path="$1"
  local mode="${2:-${KOREADER_ID_MODE:-filename}}"
  case "$mode" in
    filename) doc_id_filename "$path" ;;
    partial)  doc_id_partial "$path" ;;
    *) echo "Unknown id_mode: $mode" >&2; exit 1 ;;
  esac
}

ko_auth() {
  require_env
  local user="$KOREADER_SYNC_USER"
  local pass_hash
  pass_hash="$(md5_string "$KOREADER_SYNC_PASS")"
  echo "Testing auth for user=$user"
  curl -sS -D - \
    -H "Accept: $ACCEPT" \
    -H "X-Auth-User: $user" \
    -H "X-Auth-Key: $pass_hash" \
    "$BASE_URL/users/auth"
  echo
}

ko_put() {
  require_env
  local file="$1"
  local page="$2"
  local total="$3"
  local mode="${4:-${KOREADER_ID_MODE:-filename}}"
  if [[ ! -f "$file" ]]; then
    echo "File not found: $file" >&2
    exit 1
  fi
  local user="$KOREADER_SYNC_USER"
  local pass_hash
  pass_hash="$(md5_string "$KOREADER_SYNC_PASS")"
  local doc
  doc="$(doc_id "$file" "$mode")"
  local pct
  # bc for floating division
  pct=$(awk -v p="$page" -v t="$total" 'BEGIN { if (t<=0) print 0; else printf "%.6f", p/t }')
  echo "PUT progress doc_id=$doc page=$page total=$total pct=$pct mode=$mode"
  curl -sS -X PUT "$BASE_URL/syncs/progress" \
    -H "Accept: $ACCEPT" \
    -H "Content-Type: application/json" \
    -H "X-Auth-User: $user" \
    -H "X-Auth-Key: $pass_hash" \
    -d "{\"progress\":\"$page\",\"percentage\":$pct,\"device_id\":\"CURLTESTDEVICE\",\"document\":\"$doc\",\"device\":\"curl-client\"}"
  echo
}

ko_get() {
  require_env
  local file="$1"
  local mode="${2:-${KOREADER_ID_MODE:-filename}}"
  if [[ ! -f "$file" ]]; then
    echo "File not found: $file" >&2
    exit 1
  fi
  local user="$KOREADER_SYNC_USER"
  local pass_hash
  pass_hash="$(md5_string "$KOREADER_SYNC_PASS")"
  local doc
  doc="$(doc_id "$file" "$mode")"
  echo "GET progress doc_id=$doc mode=$mode"
  curl -sS "$BASE_URL/syncs/progress/$doc" \
    -H "Accept: $ACCEPT" \
    -H "X-Auth-User: $user" \
    -H "X-Auth-Key: $pass_hash"
  echo
}

usage() {
  cat <<EOF
KOReader Sync Protocol cURL test script.

Environment:
  KOREADER_SYNC_USER   (required)
  KOREADER_SYNC_PASS   (required, plaintext)
  KOREADER_ID_MODE     (optional: filename | partial)

Commands:
  ./test-api.sh auth
  ./test-api.sh put <file> <page> <total_pages> [id_mode]
  ./test-api.sh get <file> [id_mode]

Examples:
  ./test-api.sh auth
  ./test-api.sh put sample.epub 42 180
  ./test-api.sh get sample.epub partial

EOF
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    auth) ko_auth ;;
    put)  ko_put "$@" ;;
    get)  ko_get "$@" ;;
    ""|help|-h|--help) usage ;;
    *) echo "Unknown command: $cmd" >&2; usage; exit 1 ;;
  esac
}

main "$@"
