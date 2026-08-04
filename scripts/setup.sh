#!/usr/bin/env bash
# browser-use skill — macOS/Linux setup (developer machines).
# Installs browser-use==<pinned> and applies the enterprise hardening env vars.
set -euo pipefail

PINNED_VERSION="0.12.6"

# --- 1. Hardening: disable telemetry / cloud / version-check ---
export ANONYMIZED_TELEMETRY=false
export BROWSER_USE_CLOUD_SYNC=false
export BROWSER_USE_VERSION_CHECK=false
unset BROWSER_USE_API_KEY || true
# Persist for interactive shells (idempotent).
RC="${HOME}/.browser-use.env"
cat > "$RC" <<'EOF'
export ANONYMIZED_TELEMETRY=false
export BROWSER_USE_CLOUD_SYNC=false
export BROWSER_USE_VERSION_CHECK=false
EOF
echo "  wrote hardening env to $RC (source it from your shell rc)"

# --- 2. Pick a Python ---
PYTHON="${NETCLAW_PYTHON:-}"
for c in "$PYTHON" python3 python; do
  if [ -n "$c" ] && command -v "$c" >/dev/null 2>&1; then PYTHON="$c"; break; fi
done
[ -n "$PYTHON" ] || { echo "No Python found. Set NETCLAW_PYTHON." >&2; exit 1; }
echo "  using python: $PYTHON"

# --- 3. Install browser-use (offline wheelhouse first, else online) ---
if [ -n "${NETCLAW_WHEELHOUSE:-}" ] && [ -d "${NETCLAW_WHEELHOUSE}" ]; then
  echo "  installing browser-use==$PINNED_VERSION from wheelhouse $NETCLAW_WHEELHOUSE"
  "$PYTHON" -m pip install --no-index --find-links "$NETCLAW_WHEELHOUSE" "browser-use==$PINNED_VERSION"
else
  echo "  online install of browser-use==$PINNED_VERSION"
  "$PYTHON" -m pip install "browser-use==$PINNED_VERSION"
fi

# --- 4. Browser drivers: no downloads ---
echo "  browser: Chromium uses CDP; Firefox uses installed geckodriver"
command -v firefox >/dev/null 2>&1 || [ -n "${FIREFOX_BINARY:-}" ] || echo "  warning: Firefox not found (set FIREFOX_BINARY)"
command -v geckodriver >/dev/null 2>&1 || [ -n "${GECKODRIVER:-}" ] || echo "  warning: geckodriver not found (set GECKODRIVER)"

# --- 5. Verify ---
echo; echo "Verifying..."
browser-use doctor
echo; echo "Done. browser-use==$PINNED_VERSION installed and hardened (telemetry/cloud OFF)."
