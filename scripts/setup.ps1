# browser-use skill — Windows setup (company / air-gapped friendly)
# Installs browser-use==<pinned> and applies the enterprise hardening env vars.
# Prefers an offline wheelhouse; Firefox uses its built-in WebDriver BiDi endpoint.

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

# --- 2. Pick a Python ---
$python = $null
foreach ($cand in @($env:BROWSER_USE_PYTHON, 'python', 'python3', 'py')) {
  if ($cand -and (Get-Command $cand -ErrorAction SilentlyContinue)) { $python = $cand; break }
}
if (-not $python) { throw 'No Python found. Set BROWSER_USE_PYTHON to the bundled python.exe.' }
Write-Host "  using python: $python"

# --- 3. Install browser-use (offline wheelhouse first, else online) ---
if ($env:BROWSER_USE_WHEELHOUSE -and (Test-Path $env:BROWSER_USE_WHEELHOUSE)) {
  Write-Host "  installing browser-use==$PinnedVersion from wheelhouse $env:BROWSER_USE_WHEELHOUSE"
  & $python -m pip install --no-index --find-links "$env:BROWSER_USE_WHEELHOUSE" "browser-use==$PinnedVersion"
} else {
  Write-Host "  no BROWSER_USE_WHEELHOUSE set -> online install (needs a proxy)"
  & $python -m pip install "browser-use==$PinnedVersion"
}

# --- 4. Browsers: no driver downloads ---
Write-Host '  browser: Chromium uses CDP; Firefox uses built-in WebDriver BiDi'
$firefoxStatus = & $python (Join-Path $PSScriptRoot 'firefox-use.py') --json doctor 2>$null | ConvertFrom-Json
if ($firefoxStatus.firefox) {
  Write-Host "  firefox: $($firefoxStatus.firefox)"
} else {
  Write-Warning 'Firefox not found after PATH, registry, and standard-path discovery. Set FIREFOX_BINARY only for a non-standard or portable install.'
}

# --- 5. Verify ---
Write-Host "`nVerifying..."
& browser-use doctor
Write-Host "`nDone. browser-use==$PinnedVersion installed and hardened (telemetry/cloud OFF)."
