# browser-use skill — Windows setup (company / air-gapped friendly)
# Installs browser-use==<pinned> and applies the enterprise hardening env vars.
# Prefers an offline wheelhouse + pre-bundled Playwright browsers when provided.

$ErrorActionPreference = 'Stop'
$PinnedVersion = '0.12.6'

# --- 1. Hardening: disable telemetry / cloud / version-check (process + user) ---
$hardening = @{
  'ANONYMIZED_TELEMETRY'   = 'false'
  'BROWSER_USE_CLOUD_SYNC' = 'false'
  'BROWSER_USE_VERSION_CHECK' = 'false'
}
foreach ($k in $hardening.Keys) {
  [Environment]::SetEnvironmentVariable($k, $hardening[$k], 'Process')  # current session
  [Environment]::SetEnvironmentVariable($k, $hardening[$k], 'User')     # persisted
  Write-Host "  set $k=$($hardening[$k])"
}
# Never leave a cloud key around.
[Environment]::SetEnvironmentVariable('BROWSER_USE_API_KEY', $null, 'User')

# --- 2. Pick a Python (reuse NetClaw's bundled CPython if we can find it) ---
$python = $null
foreach ($cand in @($env:NETCLAW_PYTHON, 'python', 'python3', 'py')) {
  if ($cand -and (Get-Command $cand -ErrorAction SilentlyContinue)) { $python = $cand; break }
}
if (-not $python) { throw 'No Python found. Set NETCLAW_PYTHON to the bundled python.exe.' }
Write-Host "  using python: $python"

# --- 3. Install browser-use (offline wheelhouse first, else online) ---
if ($env:NETCLAW_WHEELHOUSE -and (Test-Path $env:NETCLAW_WHEELHOUSE)) {
  Write-Host "  installing browser-use==$PinnedVersion from wheelhouse $env:NETCLAW_WHEELHOUSE"
  & $python -m pip install --no-index --find-links "$env:NETCLAW_WHEELHOUSE" "browser-use==$PinnedVersion"
} else {
  Write-Host "  no NETCLAW_WHEELHOUSE set -> online install (needs a proxy)"
  & $python -m pip install "browser-use==$PinnedVersion"
}

# --- 4. Browser drivers: no downloads ---
Write-Host '  browser: Chromium uses CDP; Firefox uses installed geckodriver'
if (-not $env:FIREFOX_BINARY -and -not (Get-Command firefox -ErrorAction SilentlyContinue)) {
  Write-Warning 'Firefox not found; set FIREFOX_BINARY.'
}
if (-not $env:GECKODRIVER -and -not (Get-Command geckodriver -ErrorAction SilentlyContinue)) {
  Write-Warning 'geckodriver not found; set GECKODRIVER.'
}

# --- 5. Verify ---
Write-Host "`nVerifying..."
& browser-use doctor
Write-Host "`nDone. browser-use==$PinnedVersion installed and hardened (telemetry/cloud OFF)."
