---
name: browser-use
description: Automate a real, visible Chrome, Edge, Cốc Cốc, Brave, Opera, or Firefox browser when the user asks to open a website, interact with a page, fill forms, take screenshots, or extract information from the live DOM. Always launch the browser the user names through scripts/use-browser; attach browser-use over CDP for Chromium-family browsers and use scripts/firefox-use.py over WebDriver for Firefox.
---

# Browser Automation (browser-use)

Drive a real browser deterministically and index-by-index. Chromium-family browsers use
the `browser-use` CLI over Chrome DevTools Protocol (CDP); Firefox uses the bundled
`scripts/firefox-use.py` helper over local WebDriver. Both paths use installed browsers:
**pure Python, no Node/npm, no Playwright download.** Safe for internal / air-gapped machines.

> **Pinned tool version:** `browser-use==0.12.6` · **Skill version:** `1.1.2`
> To bump, see [Updating](#updating).

## Security posture (READ FIRST — enterprise hardened)

Runs fully local, no egress. `scripts/setup` disables every phone-home path.
**Do NOT use** on company machines:
- ❌ Telemetry (PostHog) — off via `ANONYMIZED_TELEMETRY=false`.
- ❌ Cloud (`browser-use cloud …`) — ships cookies/page data to browser-use.com.
- ❌ Tunnels (`browser-use tunnel …`) — opens a public URL to a local port.
- ❌ Autonomous LLM `Agent` mode — sends page state to an external model. This
  skill uses deterministic local CLIs only.

## Choose the backend

- Chrome, Edge, Cốc Cốc, Brave, or Opera → use the existing Chromium/CDP workflow below.
- Firefox → use `scripts/firefox-use.py` through the Firefox workflow below.
- Safari → unsupported; offer Chrome, Edge, Cốc Cốc, Brave, Opera, or Firefox.
- No browser preference → use Chrome. Never substitute a different named browser.

## Chromium: opening the browser — MANDATORY recipe (do NOT skip)

**ALWAYS** launch the browser with the helper FIRST, then attach browser-use to it.
This opens the **REAL, VISIBLE** browser the user named (a window appears on screen).

**NEVER** run `browser-use open <url>` on its own — that launches browser-use's own
**headless** Chromium: an *invisible*, *wrong* browser, and it needs an internet
download (fails air-gapped). That is the #1 mistake; don't make it.

```bash
# 1) Launch the browser the user named — VISIBLE, with remote debugging.
#    Prints a CDP url on its LAST line.  Options: chrome | edge | coccoc | brave | opera
scripts/use-browser.sh  edge          # macOS/Linux   (Windows: scripts\use-browser.ps1 edge)

# 2) Attach browser-use to THAT browser (reuse the CDP url it printed) and work:
browser-use --cdp-url http://127.0.0.1:9222 open https://example.com
browser-use state
browser-use click 5
```

**Rules:**
- **Open exactly the browser the user asked for** (Chromium-family, supported):
  Edge → `use-browser … edge`; Chrome → `chrome`; Cốc Cốc → `coccoc`; Brave → `brave`;
  Opera → `opera`. No preference → `chrome`. Do NOT substitute a different browser.
- It is **headed (visible) by design** — that's correct, the window should show.
- **Never** `browser-use open` alone (headless + wrong browser); **never**
  `browser-use install` (downloads Chromium — fails air-gapped).
- The daemon remembers the attached browser, so follow-up commands need no `--cdp-url`.
  If one reports it lost the browser, re-pass `--cdp-url` on that call.

> Reuse the user's LOGGED-IN Chrome (their real profile) only when the task needs
> their existing sessions: `browser-use --profile "Default" open <url>` (Chrome only).
> Otherwise always use the helper above.

## Chromium: read the page as TEXT (do NOT screenshot to "see" it)

To find/summarize what's ON a page (news, headlines, prices, article text), READ
the TEXT — the model can only reason over text, not pixels:

```bash
browser-use --cdp-url <cdp> state                 # URL, title, clickable elements + indices
browser-use --cdp-url <cdp> extract "the trending headlines"   # targeted text extraction
browser-use --cdp-url <cdp> get text <index>      # text of one element
browser-use --cdp-url <cdp> get html --selector "main"          # scoped HTML if needed
```

⚠️ **Do NOT take a screenshot just to "see"/read a page.** The engine auto-attaches
any saved image (it detects the `.png` path in the output) to the model as an inline
image; a full-page screenshot is far larger than this model's context window, so the
run **overflows and dies** ("Hội thoại quá dài" / hang). This is the #1 cause of the
browser step freezing. Use `state`/`extract`/`get text` instead.

**Screenshot only when the user explicitly wants an image file saved** — then use a
path and tell them where it is; expect the model itself can't analyse it:
`browser-use --cdp-url <cdp> screenshot ./shot.png`. (Never run `screenshot` with no
path — that dumps megabytes of base64.)

Act by index: `browser-use click 5`, `browser-use input 3 "text"`. Browser stays open.
If a command fails, run `browser-use close` to clear a broken session, then retry.

## Chromium everyday commands

Full list in [REFERENCE.md](REFERENCE.md). The ones you need most:

```bash
browser-use state                 # URL, title, elements + indices  (run before interacting)
browser-use screenshot <path.png> # ALWAYS give a path (no path = MB of base64 → agent hangs)
browser-use click <index>         # click element by index
browser-use input <index> "text"  # focus, clear, type
browser-use type "text"           # type into focused element
browser-use keys "Enter"          # send keys ("Control+a", etc.)
browser-use select <index> "opt"  # choose a dropdown option
browser-use eval "js code"        # run JS in the page, return result
browser-use get text <index>      # read element text
browser-use wait text "Loaded"    # wait for text / selector
browser-use close                 # close browser + stop daemon
```

## Chromium tips

1. **Confirm the browser first** — use the one the user named; don't silently default.
2. **Always run `state`** to get fresh indices before clicking/typing.
3. **Chain with `&&`** when you don't need intermediate output.
4. **`--headed`** shows the window; aliases: `bu`.
5. **`browser-use close`** when done.

## Firefox workflow

Route Firefox through the bundled WebDriver helper, never through Chromium CDP:

```bash
scripts/use-browser.sh firefox
scripts/firefox-use.py open https://example.com
scripts/firefox-use.py state
scripts/firefox-use.py click 5
```

On Windows:

```powershell
scripts\use-browser.ps1 firefox
& $env:NETCLAW_PYTHON scripts\firefox-use.py open https://example.com
& $env:NETCLAW_PYTHON scripts\firefox-use.py state
```

Require installed Firefox and `geckodriver` on `PATH`, or set `FIREFOX_BINARY` and
`GECKODRIVER`. Open a visible browser with the most recently used managed profile;
when none exists, automatically create and reuse an empty managed profile named
`automation`. Do not claim to attach to a normally-open Firefox instance.

Firefox supports three profile modes:

- No `--profile`: reuse the most recently used managed profile, or automatically create persistent managed profile `automation` when none exist.
- `--profile NATIVE_NAME`: clone a closed native profile for this session, then delete the clone on close.
- `--profile MANAGED_NAME`: reuse a persistent automation profile across browser restarts.

By default, Firefox automation state lives under `${TMPDIR:-/tmp}/browser-use-firefox`.
Managed profiles use the same temporary root on normal Firefox, but use the
Snap/Flatpak-approved profile directory when sandboxed. Set `BROWSER_USE_HOME`
and `FIREFOX_PROFILE_HOME` to override these locations.

`profile list` recognizes both legacy `profiles.ini` names and modern Firefox Profile
Groups names shown in the UI, such as `Work`; either native name can be passed to
`--profile` and is cloned only after Firefox is closed.

Create and manage persistent profiles without modifying the native source:

```bash
scripts/firefox-use.py profile list
scripts/firefox-use.py profile create work
scripts/firefox-use.py profile create social --from "default-release"
scripts/firefox-use.py --profile social open https://example.com
scripts/firefox-use.py profile delete work
```

Reject a locked native profile and ask the user to close Firefox before cloning. A
managed profile may be used by only one session at a time. `close` preserves managed
profiles; `profile delete` removes them. Managed profiles contain sensitive cookies
and login tokens, so protect their storage like a native browser profile.

Reuse the same `--session` to continue controlling an existing Firefox window. When
multiple managed profiles exist, automatically reuse the most recently opened one.
An explicit `--profile NAME` always wins and becomes the default for the next automatic
launch. If the selected profile is locked, report the lock instead of switching accounts.

Run `state` before every indexed interaction and again after navigation, tab switches,
or DOM updates. Use `state`, `get text`, scoped `get html`, or `eval` to read pages;
Firefox does not implement upstream `extract`. Take screenshots only when the user
explicitly requests an image file, and always pass a path.

```bash
scripts/firefox-use.py state
scripts/firefox-use.py get text 3
scripts/firefox-use.py get html --selector main
scripts/firefox-use.py input 4 "NetClaw"
scripts/firefox-use.py screenshot ./shot.png
scripts/firefox-use.py close
```

Put global options before the command: `--session NAME`, `--profile NAME`, or `--json`.
Firefox is headed by default; set `FIREFOX_USE_HEADLESS=true` only for automation tests.
On Linux, headed mode requires `DISPLAY` or `WAYLAND_DISPLAY`.

## Troubleshooting

### Chromium

- `doctor` says not installed → run `scripts/setup` (see [setup.json](setup.json)).
- "Could not find Chrome" / tries to download Chromium → you skipped step 1; launch
  the browser with `scripts/use-browser …` and attach via `--cdp-url` (never run
  `browser-use install` on an air-gapped machine — it downloads Chromium).
- Element not found → `browser-use scroll down` then `browser-use state` again.

### Firefox

- Run `scripts/firefox-use.py doctor` to verify Firefox, geckodriver, profile storage, and display readiness.
- If a native profile is locked, close normal Firefox before cloning it.
- If an element index is stale, run `state` again instead of retrying the old index.

## Updating

Tool version is pinned in `setup.json` and `_meta.json`; keep both in sync when
bumping. Note: `0.13.x+` adds a Rust `browser-harness` dependency and a `--channel`
flag; `0.12.6` is intentionally pinned as the lightest pure-Python line for air-gapped
installs. Re-check `.env.example` for new telemetry/cloud env vars before shipping a
new pin.

See [REFERENCE.md](REFERENCE.md) for the full Chromium and Firefox command references.
