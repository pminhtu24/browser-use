# use-browser.ps1 — launch a Chromium-family browser with remote debugging, then
# print a CDP url for:  browser-use --cdp-url <url> open <site>
#
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\use-browser.ps1" chrome
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\use-browser.ps1" edge 9333
#
# Detection mirrors the engine's fast `browser_open` tool: env vars WITH hardcoded
# fallbacks, plus the Windows "App Paths" registry and PATH — so it finds the
# browser even on non-standard installs. Uses an already-installed browser (no
# download) -> air-gapped safe. Firefox uses its built-in WebDriver BiDi endpoint.

param(
  [Parameter(Mandatory=$true)][ValidateSet('chrome','edge','coccoc','brave','opera','firefox','safari')][string]$Browser,
  [int]$Port = 9222
)
$ErrorActionPreference = 'Stop'

# Base dirs with hardcoded fallbacks (env var may be absent under odd shells).
$pf    = if ($env:ProgramFiles)        { $env:ProgramFiles }        else { 'C:\Program Files' }
$pf86  = if (${env:ProgramFiles(x86)}) { ${env:ProgramFiles(x86)} } else { 'C:\Program Files (x86)' }
$local = if ($env:LOCALAPPDATA)        { $env:LOCALAPPDATA }        else { "$env:USERPROFILE\AppData\Local" }

function Find-Exe([string[]]$paths, [string]$exeName) {
  foreach ($p in $paths) { if ($p -and (Test-Path $p)) { return $p } }
  # Windows "App Paths" registry — the canonical record of where an exe lives.
  foreach ($root in @('HKLM:', 'HKCU:')) {
    foreach ($view in @('SOFTWARE', 'SOFTWARE\WOW6432Node')) {
      try {
        $val = (Get-ItemProperty "$root\$view\Microsoft\Windows\CurrentVersion\App Paths\$exeName" -ErrorAction Stop).'(default)'
        if ($val -and (Test-Path $val)) { return $val }
      } catch { }
    }
  }
  $cmd = Get-Command $exeName -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -and (Test-Path $cmd.Source)) { return $cmd.Source }
  return $null
}

switch ($Browser) {
  'safari'  { Write-Error "Safari is NOT supported (WebKit, no Chromium CDP). Use chrome/edge/coccoc/brave/opera."; exit 2 }
  'firefox' {
    $python = if ($env:BROWSER_USE_PYTHON) { $env:BROWSER_USE_PYTHON } else {
      @('python', 'python3', 'py') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
    }
    if (-not $python) { Write-Error 'Python not found. Set BROWSER_USE_PYTHON.'; exit 3 }
    & $python (Join-Path $PSScriptRoot 'firefox-use.py') start
    exit $LASTEXITCODE
  }
  'chrome'  { $exe = Find-Exe @("$pf\Google\Chrome\Application\chrome.exe", "$pf86\Google\Chrome\Application\chrome.exe", "$local\Google\Chrome\Application\chrome.exe") 'chrome.exe' }
  'edge'    { $exe = Find-Exe @("$pf86\Microsoft\Edge\Application\msedge.exe", "$pf\Microsoft\Edge\Application\msedge.exe") 'msedge.exe' }
  'coccoc'  { $exe = Find-Exe @("$local\CocCoc\Browser\Application\browser.exe", "$pf\CocCoc\Browser\Application\browser.exe", "$pf86\CocCoc\Browser\Application\browser.exe") 'browser.exe' }
  'brave'   { $exe = Find-Exe @("$pf\BraveSoftware\Brave-Browser\Application\brave.exe", "$pf86\BraveSoftware\Brave-Browser\Application\brave.exe", "$local\BraveSoftware\Brave-Browser\Application\brave.exe") 'brave.exe' }
  'opera'   { $exe = Find-Exe @("$local\Programs\Opera\opera.exe", "$local\Programs\Opera\launcher.exe", "$pf\Opera\opera.exe", "$pf86\Opera\opera.exe") 'opera.exe' }
}
if (-not $exe) { Write-Error "$Browser not found on this machine. Try another (chrome/edge/coccoc/brave/opera/firefox)."; exit 3 }

# Isolated temp profile so we never touch the user's real logins.
$profileDir = Join-Path $env:TEMP "browser-use-$Browser-$Port"
$null = New-Item -ItemType Directory -Force -Path $profileDir
Start-Process -FilePath $exe -ArgumentList @(
  "--remote-debugging-port=$Port", "--user-data-dir=`"$profileDir`"",
  '--no-first-run', '--no-default-browser-check') | Out-Null

$cdp = "http://127.0.0.1:$Port"
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
  try { Invoke-WebRequest "$cdp/json/version" -UseBasicParsing -TimeoutSec 1 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 300 }
}
if (-not $ready) { Write-Error "Failed to launch $Browser`: CDP did not become ready at $cdp"; exit 4 }
Write-Host "Launched $Browser ($exe)"
Write-Host "Next: browser-use --cdp-url $cdp open <url>"
$cdp   # last line = CDP url (for scripting)
