# browser-use CLI reference (0.12.6, hardened subset)

Deterministic, index-based browser control over CDP. Pure Python — no Node, no
Playwright, no network egress beyond the pages you open. Cloud / tunnel commands
exist upstream but are **disabled on purpose** — see
[SKILL.md](SKILL.md#security-posture-read-first--enterprise-hardened).

## Global options (real flags in 0.12.6)

| Option | Description |
|--------|-------------|
| `--cdp-url <url>` | Attach to an already-running browser via CDP (how we drive Chrome/Edge) |
| `--profile [NAME]` | Launch the installed **Google Chrome** with a saved profile (bare = "Default") |
| `--headed` | Show the browser window |
| `--session NAME` | Target a named session (default: "default") — for parallel browsers |
| `--json` | Machine-readable output |

> There is **no `--channel` flag** in 0.12.6. Pick the browser by launching it via
> `scripts/use-browser …` and attaching with `--cdp-url` (Chrome/Edge), or use
> `--profile` for the installed Chrome. See SKILL.md → "Choosing the browser".

On Windows, never open a `.ps1` directly. Execute setup and browser launchers with
PowerShell so Windows does not show the “Open with” dialog:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\use-browser.ps1" chrome
```

Invoke Firefox commands through Python, not by opening the `.py` file:

```powershell
& "$env:BROWSER_USE_PYTHON" ".\scripts\firefox-use.py" state
```

## Navigation

```bash
browser-use open <url>                    # navigate (attach first via --cdp-url)
browser-use back                          # history back
browser-use scroll down                   # scroll (--amount N for pixels)
browser-use scroll up
browser-use switch <index>                # switch to tab by index
browser-use close-tab <index>             # close a tab
```

## Page state (run `state` before interacting)

```bash
browser-use state                         # URL, title, clickable elements + indices
browser-use screenshot <path.png>         # ALWAYS pass a path. No path = multi-MB base64 dumped
                                          # into the tool output → floods context, the agent hangs.
```

## Interactions (use indices from `state`)

```bash
browser-use click <index>                 # click by index
browser-use type "text"                   # type into focused element
browser-use input <index> "text"          # click, clear, then type
browser-use keys "Enter"                  # keys, e.g. "Control+a"
browser-use select <index> "option"       # select dropdown option
browser-use upload <index> <path>         # upload a local file
browser-use hover <index>
browser-use dblclick <index>
browser-use rightclick <index>
```

## Read / extract

```bash
browser-use eval "js code"                # run JS, return result
browser-use extract "<what to pull>"      # structured extraction from the page
browser-use get title
browser-use get html [--selector "h1"]
browser-use get text <index>
browser-use get value <index>
browser-use get attributes <index>
browser-use get bbox <index>
```

## Wait

```bash
browser-use wait selector "css"           # --state visible|hidden|attached|detached, --timeout ms
browser-use wait text "text"
```

## Cookies (local only)

```bash
browser-use cookies get [--url <url>]
browser-use cookies set <name> <value>
browser-use cookies clear [--url <url>]
browser-use cookies export <file>
browser-use cookies import <file>
```

## Session / diagnostics

```bash
browser-use close                         # close browser + stop daemon
browser-use close --all                   # close everything
browser-use sessions                      # list active sessions
browser-use doctor                        # install + config check (verifies telemetry/cloud OFF)
browser-use profile list                  # list installed Chrome profiles (for --profile)
```

---

## Removed on purpose (do NOT use)

- `cloud …` — cloud browser + REST to browser-use.com (ships cookies/page data)
- `tunnel …` — opens a public Cloudflare URL to a local port
- `install` — downloads Chromium via the internet (breaks on air-gapped machines;
  we use the installed Chrome/Edge instead)
- Python `Agent(...)` autonomous mode — sends page state to an external LLM

---

# Firefox CLI reference (skill 1.1.2)

Use `scripts/firefox-use.py` for Firefox. It speaks local W3C WebDriver through the
bundled Windows x64 geckodriver, `GECKODRIVER`, or a driver on `PATH`, in that order,
and keeps the same deterministic, index-based workflow. The bundled driver requires
no runtime download, installation, `PATH` change, or administrator access.

Global options must precede the command:

| Option | Description |
|--------|-------------|
| `--session NAME` | Select a persistent named session (default `default`) |
| `--profile NAME` | Use a managed profile, or temporarily clone a closed native profile |
| `--json` | Emit machine-readable JSON |
| `--headed` | Compatibility flag; Firefox is already visible by default |

```bash
# Browser/session lifecycle
scripts/firefox-use.py start
scripts/firefox-use.py sessions
scripts/firefox-use.py --session work open https://example.com
scripts/firefox-use.py --session work close
scripts/firefox-use.py close --all
scripts/firefox-use.py doctor
scripts/firefox-use.py profile list
scripts/firefox-use.py profile create work
scripts/firefox-use.py profile create social --from default-release
scripts/firefox-use.py profile delete work

# Navigation, state, tabs
scripts/firefox-use.py open <url>
scripts/firefox-use.py back
scripts/firefox-use.py scroll up|down [--amount 600]
scripts/firefox-use.py state
scripts/firefox-use.py switch <tab-index>
scripts/firefox-use.py close-tab [tab-index]

# Interactions
scripts/firefox-use.py click <index>
scripts/firefox-use.py input <index> "text"
scripts/firefox-use.py type "text"
scripts/firefox-use.py keys "Control+a"
scripts/firefox-use.py select <index> "value-or-label"
scripts/firefox-use.py upload <index> <path>
scripts/firefox-use.py hover|dblclick|rightclick <index>

# Read and wait
scripts/firefox-use.py eval "document.title"
scripts/firefox-use.py get title|url|html
scripts/firefox-use.py get html --selector "main"
scripts/firefox-use.py get text|value|attributes|bbox <index>
scripts/firefox-use.py wait text "Success" [--timeout 5000]
scripts/firefox-use.py wait selector ".ready" [--state visible|hidden|attached|detached] [--timeout 5000]

# Cookies and files
scripts/firefox-use.py cookies get [--url <url>]
scripts/firefox-use.py cookies set <name> <value> [--domain <domain>] [--path /] [--secure] [--http-only] [--same-site Strict|Lax|None] [--expires <unix>]
scripts/firefox-use.py cookies clear [--url <url>]
scripts/firefox-use.py cookies export <file.json>
scripts/firefox-use.py cookies import <file.json>
scripts/firefox-use.py screenshot <path.png>
```

`state` includes visible interactive elements from the document, open shadow roots,
and accessible iframes. Its indices expire when the DOM changes. Cookie export/clear
covers origins visited by that Firefox session.

## Firefox profiles

`profile list` marks Firefox-owned profiles as `native` and persistent automation
profiles as `managed`. Native names clone the closed source for one session and
delete that temporary clone on close. Managed profiles live under
`FIREFOX_PROFILE_HOME` (or `.firefox-profiles` in this package), survive `close`,
and must be removed explicitly with `profile delete`.

Native discovery supports both legacy `profiles.ini` names and modern Firefox
Profile Groups names shown in the UI, such as `Work`.

Managed names cannot collide with native names, and one managed profile cannot be
opened by two sessions simultaneously. Creating from a native profile requires
Firefox to be closed and never writes changes back to the source profile. Without
`--profile`, always reuse the persistent managed profile `automation`, creating it when
absent. An explicit managed profile applies only to that launch. Report a selected
profile lock instead of switching accounts.

On Linux, headed Firefox requires `DISPLAY` or `WAYLAND_DISPLAY`. If the tool runner
filters GUI variables, forward `DISPLAY`, `XAUTHORITY`, and `DBUS_SESSION_BUS_ADDRESS`;
do not assume `:0` or start Xvfb. Firefox does not implement `extract`; use `eval`,
`get text`, or `get html`.
