#!/usr/bin/env bash
# use-browser.sh — launch the browser the USER asked for with remote debugging,
# then print a CDP url for:  browser-use --cdp-url <url> open <site>
#
#   scripts/use-browser.sh chrome        # or: edge
#   scripts/use-browser.sh edge 9333      # custom port
#
# Uses the browser ALREADY INSTALLED (no download) -> air-gapped safe.
# Firefox uses its built-in WebDriver BiDi endpoint; Chromium keeps CDP.
set -euo pipefail

BROWSER="${1:-}"; PORT="${2:-9222}"
case "$BROWSER" in
  safari)  echo "Safari is NOT supported (WebKit, no Chromium CDP). Use chrome/edge/coccoc/brave/opera." >&2; exit 2 ;;
  firefox)
    PYTHON="${BROWSER_USE_PYTHON:-$(command -v python3 || command -v python || true)}"
    [ -n "$PYTHON" ] || { echo "Python not found. Set BROWSER_USE_PYTHON." >&2; exit 3; }
    exec "$PYTHON" "$(dirname "$0")/firefox-use.py" start
    ;;
  chrome|edge|coccoc|brave|opera) : ;;
  *) echo "Usage: $0 <chrome|edge|coccoc|brave|opera|firefox> [port]" >&2; exit 1 ;;
esac

find_exe() { for p in "$@"; do [ -x "$p" ] && { echo "$p"; return 0; }; done; return 1; }
first_cmd() { for c in "$@"; do command -v "$c" 2>/dev/null && return 0; done; return 1; }

if [ "$(uname)" = "Darwin" ]; then
  case "$BROWSER" in
    chrome) EXE=$(find_exe "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "/Applications/Chromium.app/Contents/MacOS/Chromium") ;;
    edge)   EXE=$(find_exe "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge") ;;
    coccoc) EXE=$(find_exe "/Applications/CocCoc.app/Contents/MacOS/CocCoc" "/Applications/Cốc Cốc.app/Contents/MacOS/Cốc Cốc") ;;
    brave)  EXE=$(find_exe "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser") ;;
    opera)  EXE=$(find_exe "/Applications/Opera.app/Contents/MacOS/Opera") ;;
  esac
else
  case "$BROWSER" in
    chrome) EXE=$(first_cmd google-chrome google-chrome-stable chromium chromium-browser) ;;
    edge)   EXE=$(first_cmd microsoft-edge microsoft-edge-stable msedge) ;;
    coccoc) EXE=$(first_cmd coccoc-browser) ;;
    brave)  EXE=$(first_cmd brave-browser brave) ;;
    opera)  EXE=$(first_cmd opera opera-stable) ;;
  esac
fi
[ -n "${EXE:-}" ] || { echo "$BROWSER not found on this machine. Try another (chrome/edge/coccoc/brave/opera)." >&2; exit 3; }

PROFILE="${TMPDIR:-/tmp}/browser-use-$BROWSER-$PORT"
mkdir -p "$PROFILE"
if command -v setsid >/dev/null 2>&1; then
  setsid "$EXE" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE" \
         --no-first-run --no-default-browser-check >/dev/null 2>&1 &
else
  nohup "$EXE" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE" \
        --no-first-run --no-default-browser-check >/dev/null 2>&1 &
fi

CDP="http://127.0.0.1:$PORT"
READY=false
for _ in $(seq 1 30); do
  if curl -fsS "$CDP/json/version" >/dev/null 2>&1; then READY=true; break; fi
  sleep 0.3
done
$READY || { echo "Failed to launch $BROWSER: CDP did not become ready at $CDP" >&2; exit 4; }
echo "Launched $BROWSER ($EXE)" >&2
echo "Next: browser-use --cdp-url $CDP open <url>" >&2
echo "$CDP"   # last line = CDP url
