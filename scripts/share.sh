#!/usr/bin/env bash
# share-it CLI helper.
#
# Usage:
#   share <file>            upload a file, print the link
#   some_cmd | share -      upload stdin as stdout.txt
#
# Install: source this file from your shell rc, e.g.
#   echo 'source /path/to/share-it/scripts/share.sh' >> ~/.zshrc
#
# Config: set SHARE_IT_HOST to point at your instance
#   export SHARE_IT_HOST=https://ahsans-mac-mini.tailb5c9c.ts.net:3050
# Defaults to http://localhost:3050.

share() {
  local host="${SHARE_IT_HOST:-http://localhost:3050}"
  local url

  if [ "$1" = "-" ] || { [ -z "$1" ] && [ ! -t 0 ]; }; then
    # Read from stdin -> upload as stdout.txt. `Accept: text/plain` makes the
    # server reply with just the URL (no JSON parsing needed).
    url=$(curl -sf -H "Accept: text/plain" \
            -F "file=@-;filename=stdout.txt;type=text/plain" \
            "$host/upload") || { echo "share: upload failed" >&2; return 1; }
  elif [ -n "$1" ] && [ -f "$1" ]; then
    url=$(curl -sf -H "Accept: text/plain" -F "file=@$1" "$host/upload") \
      || { echo "share: upload failed" >&2; return 1; }
  else
    echo "usage: share <file>   |   some_cmd | share -" >&2
    return 2
  fi

  url="${url%$'\n'}"
  [ -z "$url" ] && { echo "share: empty response" >&2; return 1; }
  echo "$url"
  # Copy to clipboard when available (macOS pbcopy / Linux xclip / Wayland wl-copy).
  if command -v pbcopy >/dev/null 2>&1; then printf '%s' "$url" | pbcopy
  elif command -v wl-copy >/dev/null 2>&1; then printf '%s' "$url" | wl-copy
  elif command -v xclip >/dev/null 2>&1; then printf '%s' "$url" | xclip -selection clipboard
  fi
}
