#!/usr/bin/env python3
"""Deterministic Firefox automation through WebDriver BiDi."""

from __future__ import annotations

import argparse
import asyncio
import base64
import configparser
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp
from aiohttp import web

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PROFILE_LOCKS = ("parent.lock", ".parentlock", "lock")
KEYS = {
    "Null": "\ue000", "Backspace": "\ue003", "Tab": "\ue004", "Enter": "\ue007",
    "Shift": "\ue008", "Control": "\ue009", "Alt": "\ue00a", "Escape": "\ue00c",
    "ArrowLeft": "\ue012", "ArrowUp": "\ue013", "ArrowRight": "\ue014",
    "ArrowDown": "\ue015", "Delete": "\ue017", "Meta": "\ue03d",
}


def home() -> Path:
    if configured := os.environ.get("FIREFOX_STATE_HOME"):
        root = Path(configured).expanduser()
    elif configured := os.environ.get("BROWSER_USE_HOME"):
        root = Path(configured).expanduser() / "firefox"
    else:
        root = Path(tempfile.gettempdir()) / "browser-use-firefox" / "state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def secure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def safe_session(session: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", session)


def state_path(session: str) -> Path:
    return home() / f"{safe_session(session)}.json"


def load_state(session: str) -> dict:
    try:
        return json.loads(state_path(session).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(session: str, state: dict) -> None:
    path = state_path(session)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


async def _request(websocket_url: str, method: str, params: dict, timeout: float):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as client:
            async with client.ws_connect(websocket_url, max_msg_size=0) as websocket:
                await websocket.send_json({"id": 1, "method": method, "params": params})
                while True:
                    message = await asyncio.wait_for(websocket.receive(), timeout)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(message.data)
                        if data.get("id") != 1:
                            continue
                        if data.get("type") == "error":
                            detail = data.get("message") or data.get("error") or "WebDriver BiDi command failed"
                            if data.get("error") == "stale element reference":
                                detail = "Element is stale; run state again."
                            raise RuntimeError(detail)
                        return data.get("result")
                    if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                        raise RuntimeError("Firefox closed the WebDriver BiDi connection")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
        raise RuntimeError(f"Firefox WebDriver BiDi is unavailable: {error}") from error


def request(websocket_url: str, method: str, params: dict | None = None, timeout: float = 10):
    return asyncio.run(_request(websocket_url, method, params or {}, timeout))


def broker_request(state: dict, method: str, params: dict | None = None, timeout: float = 10):
    body = json.dumps({"method": method, "params": params or {}}).encode()
    request_value = Request(
        f"http://127.0.0.1:{state['broker_port']}/command", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Firefox-Token": state["broker_token"]},
    )
    try:
        with urlopen(request_value, timeout=timeout) as response:
            value = json.load(response)
    except HTTPError as error:
        try:
            value = json.load(error)
            detail = value.get("error")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"Firefox BiDi broker HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Firefox BiDi broker is unavailable: {getattr(error, 'reason', error)}") from error
    return value.get("result")


def call(state: dict, method: str, params: dict | None = None, timeout: float = 10):
    return broker_request(state, method, params, timeout)


def local_value(value):
    if isinstance(value, dict) and "sharedId" in value:
        return {"sharedId": value["sharedId"]}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, (int, float)):
        return {"type": "number", "value": value}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, list):
        return {"type": "array", "value": [local_value(item) for item in value]}
    if isinstance(value, dict):
        return {"type": "object", "value": [[str(key), local_value(item)] for key, item in value.items()]}
    raise RuntimeError(f"Unsupported script argument: {type(value).__name__}")


def remote_value(value):
    if not isinstance(value, dict) or "type" not in value:
        return value
    kind = value["type"]
    if kind in {"null", "undefined"}:
        return None
    if kind == "node":
        return {"sharedId": value["sharedId"]}
    if kind in {"array", "set"}:
        return [remote_value(item) for item in value.get("value", [])]
    if kind in {"object", "map"}:
        return {
            remote_value(key) if isinstance(key, dict) else key: remote_value(item)
            for key, item in value.get("value", [])
        }
    return value.get("value")


def execute(state: dict, script: str, args: list | None = None, context: str | None = None):
    result = call(state, "script.callFunction", {
        "functionDeclaration": f"function(){{{script}}}",
        "awaitPromise": True,
        "target": {"context": context or current_context(state)["context"]},
        "arguments": [local_value(value) for value in (args or [])],
        "resultOwnership": "none",
        "serializationOptions": {"maxObjectDepth": 10, "maxDomDepth": 1, "includeShadowTree": "open"},
    })
    if result.get("type") == "exception":
        details = result.get("exceptionDetails", {})
        raise RuntimeError(details.get("text") or remote_value(details.get("exception")) or "JavaScript evaluation failed")
    return remote_value(result["result"])


async def run_broker(websocket_url: str, port: int, token: str) -> None:
    client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
    runner = None
    try:
        websocket = await client.ws_connect(websocket_url, max_msg_size=0)
        command_id = 0
        lock = asyncio.Lock()
        stopped = asyncio.Event()

        async def send(method: str, params: dict, timeout: float = 30):
            nonlocal command_id
            async with lock:
                command_id += 1
                current_id = command_id
                await websocket.send_json({"id": current_id, "method": method, "params": params})
                while True:
                    message = await asyncio.wait_for(websocket.receive(), timeout)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(message.data)
                        if data.get("id") != current_id:
                            continue
                        if data.get("type") == "error":
                            detail = data.get("message") or data.get("error") or "WebDriver BiDi command failed"
                            if data.get("error") == "stale element reference":
                                detail = "Element is stale; run state again."
                            raise RuntimeError(detail)
                        return data.get("result")
                    if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                        raise RuntimeError("Firefox closed the WebDriver BiDi connection")

        session = await send("session.new", {"capabilities": {"alwaysMatch": {"browserName": "firefox"}}})

        async def command(request_value: web.Request) -> web.Response:
            if not secrets.compare_digest(request_value.headers.get("X-Firefox-Token", ""), token):
                return web.json_response({"error": "Forbidden"}, status=403)
            try:
                payload = await request_value.json()
                method = payload["method"]
                if method == "__status__":
                    result = {"sessionId": session["sessionId"]}
                elif method == "__stop__":
                    try:
                        result = await send("browser.close", {})
                    finally:
                        asyncio.get_running_loop().call_later(0.1, stopped.set)
                else:
                    result = await send(method, payload.get("params") or {})
                return web.json_response({"result": result})
            except Exception as error:
                return web.json_response({"error": str(error)}, status=500)

        application = web.Application()
        application.router.add_post("/command", command)
        runner = web.AppRunner(application, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        await stopped.wait()
    finally:
        if runner:
            await runner.cleanup()
        await client.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def find_firefox() -> str | None:
    if binary := os.environ.get("FIREFOX_BINARY"):
        if Path(binary).expanduser().is_file():
            return str(Path(binary).expanduser())
        if found := shutil.which(binary):
            return found
    found = shutil.which("firefox") or shutil.which("firefox.exe")
    if found:
        return found
    if os.name == "nt":
        import winreg
        key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe"
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, key_name, 0, winreg.KEY_READ | view) as key:
                        path = str(winreg.QueryValue(key, None)).strip('"')
                    if Path(path).is_file():
                        return path
                except OSError:
                    pass
    candidates = [
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Mozilla Firefox", "firefox.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Mozilla Firefox", "firefox.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Mozilla Firefox", "firefox.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Mozilla Firefox", "firefox.exe"),
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def profile_ini() -> Path | None:
    if configured := os.environ.get("FIREFOX_PROFILES_INI"):
        path = Path(configured)
        return path if path.is_file() else None
    native = Path.home() / ".mozilla" / "firefox" / "profiles.ini"
    snap = Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox" / "profiles.ini"
    flatpak = Path.home() / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox" / "profiles.ini"
    candidates = [native, snap, flatpak] + [
        Path.home() / "Library" / "Application Support" / "Firefox" / "profiles.ini",
        Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "profiles.ini",
    ]
    return next((path for path in candidates if path.is_file()), None)


def profile_home() -> Path:
    if configured := os.environ.get("FIREFOX_PROFILE_HOME"):
        return secure_directory(Path(configured).expanduser())
    installation = firefox_installation()
    if installation == "snap":
        root = Path.home() / "snap" / "firefox" / "common" / ".browser-use-profiles"
    elif installation == "flatpak":
        root = Path.home() / ".var" / "app" / "org.mozilla.firefox" / ".browser-use-profiles"
    else:
        root = Path(tempfile.gettempdir()) / "browser-use-firefox" / "profiles"
    return secure_directory(root)


def validate_profile_name(name: str) -> str:
    if not PROFILE_NAME.fullmatch(name):
        raise RuntimeError("Profile name must be 1-64 characters: letters, numbers, dot, underscore, or dash.")
    return name


def profile_locked(path: Path) -> bool:
    return any((path / name).exists() or (path / name).is_symlink() for name in PROFILE_LOCKS)


def firefox_profiles() -> list[dict]:
    ini = profile_ini()
    if not ini:
        return []
    config = configparser.ConfigParser()
    config.read(ini, encoding="utf-8")
    profiles = []
    stores = set()
    for section in config.sections():
        if not section.startswith("Profile") or "Path" not in config[section]:
            continue
        value = config[section]
        path = Path(value["Path"])
        if value.get("IsRelative", "1") == "1":
            path = ini.parent / path
        if value.get("StoreID"):
            stores.add(value["StoreID"])
        profiles.append({
            "kind": "native", "name": value.get("Name", section), "path": str(path),
            "default": value.get("Default") == "1", "locked": profile_locked(path),
        })
    default_paths = {Path(item["path"]).resolve() for item in profiles if item["default"]}
    grouped = []
    for store in stores:
        database = ini.parent / "Profile Groups" / f"{store}.sqlite"
        if not database.is_file():
            continue
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                rows = connection.execute("SELECT path, name FROM Profiles ORDER BY id").fetchall()
        except (OSError, sqlite3.Error):
            continue
        for raw_path, name in rows:
            path = Path(raw_path)
            if not path.is_absolute():
                path = ini.parent / path
            grouped.append({
                "kind": "native", "name": name, "path": str(path),
                "default": path.resolve() in default_paths, "locked": profile_locked(path),
            })
    if grouped:
        grouped_paths = {Path(item["path"]).resolve() for item in grouped}
        return grouped + [item for item in profiles if Path(item["path"]).resolve() not in grouped_paths]
    return profiles


def native_profile(name: str) -> dict:
    profiles = firefox_profiles()
    selected = next((p for p in profiles if p["name"].casefold() == name.casefold()), None)
    if name.casefold() in {"default", "default-release"}:
        selected = selected or next((p for p in profiles if p["default"]), None)
    if not selected:
        raise RuntimeError(f"Firefox profile not found: {name}. Run profile list.")
    return selected


def clone_profile(name: str, session: str) -> Path:
    selected = native_profile(name)
    source = Path(selected["path"])
    if profile_locked(source) and firefox_lock_stale(source):
        clear_firefox_locks(source)
    if profile_locked(source):
        raise RuntimeError(f"Firefox profile is locked: {selected['name']}. Close Firefox first.")
    target = secure_directory(profile_home() / "temporary") / safe_session(session)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*PROFILE_LOCKS))
    return target


def managed_root() -> Path:
    return secure_directory(profile_home() / "managed")


def managed_dir(name: str) -> Path:
    return managed_root() / validate_profile_name(name)


def read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def managed_metadata(path: Path) -> dict:
    return read_json_file(path / "meta.json")


def write_private_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def managed_profiles() -> list[dict]:
    output = []
    for path in sorted(managed_root().iterdir()):
        if not path.is_dir() or path.name.startswith(".") or not (path / "profile").is_dir():
            continue
        meta = managed_metadata(path)
        lock = read_json_file(path / ".browser-use-lock")
        output.append({
            "kind": "managed", "name": path.name, "path": str(path / "profile"),
            "source": meta.get("source"), "locked": profile_locked(path / "profile"),
            "session": lock.get("session"),
        })
    return output


def resolve_profile(profile: str | None) -> str | None:
    if profile:
        return profile
    if not managed_dir("automation").is_dir():
        create_managed_profile("automation", None)
    return "automation"


def mark_managed_profile_used(name: str) -> None:
    path = managed_dir(name)
    metadata = managed_metadata(path)
    metadata["lastUsedAt"] = time.time_ns()
    write_private_json(path / "meta.json", metadata)


def create_managed_profile(name: str, source_name: str | None) -> dict:
    validate_profile_name(name)
    if any(item["name"].casefold() == name.casefold() for item in firefox_profiles()):
        raise RuntimeError(f"Managed profile name conflicts with native Firefox profile: {name}")
    if any(item["name"].casefold() == name.casefold() for item in managed_profiles()):
        raise RuntimeError(f"Managed Firefox profile already exists: {name}")
    target = managed_dir(name)
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"Managed Firefox profile already exists: {name}")
    source = None
    if source_name:
        selected = native_profile(source_name)
        source = Path(selected["path"])
        if profile_locked(source):
            raise RuntimeError(f"Firefox profile is locked: {selected['name']}. Close Firefox first.")
    temporary = managed_root() / f".{name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        temporary.mkdir(mode=0o700)
        destination = temporary / "profile"
        if source:
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*PROFILE_LOCKS))
        else:
            destination.mkdir(mode=0o700)
        write_private_json(temporary / "meta.json", {
            "name": name, "source": source_name, "sourcePath": str(source) if source else None,
            "createdAt": int(time.time()),
        })
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"kind": "managed", "name": name, "path": str(target / "profile"), "source": source_name}


def managed_in_use(name: str) -> str | None:
    for path in home().glob("*.json"):
        state = load_state(path.stem)
        if state.get("profile_kind") == "managed" and state.get("profile") == name and driver_alive(state):
            return path.stem
    return None


def delete_managed_profile(name: str) -> dict:
    target = managed_dir(name)
    if target.is_symlink() or not target.is_dir() or target.resolve().parent != managed_root().resolve():
        raise RuntimeError(f"Managed Firefox profile not found or unsafe: {name}")
    if session := managed_in_use(name):
        raise RuntimeError(f"Managed Firefox profile is in use by session: {session}. Close it first.")
    lock_state = read_json_file(target / ".browser-use-lock")
    if process_alive(lock_state.get("pid")):
        raise RuntimeError(f"Managed Firefox profile is being opened by session: {lock_state.get('session') or 'unknown'}")
    (target / ".browser-use-lock").unlink(missing_ok=True)
    if profile_locked(target / "profile") and firefox_lock_stale(target / "profile"):
        clear_managed_firefox_locks(name)
    if profile_locked(target / "profile"):
        raise RuntimeError(f"Managed Firefox profile is locked: {name}. Close Firefox first.")
    shutil.rmtree(target)
    return {"deleted": name}


def headless_enabled() -> bool:
    return os.environ.get("FIREFOX_USE_HEADLESS", "").lower() in {"1", "true", "yes"}


def headed_ready() -> bool:
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def firefox_installation() -> str:
    binary = str(find_firefox() or "").replace("\\", "/")
    if "/snap/" in binary:
        return "snap"
    if "/flatpak/" in binary or "/.var/app/org.mozilla.firefox/" in binary:
        return "flatpak"
    value = str(profile_ini() or "").replace("\\", "/")
    if "/snap/firefox/common/" in value:
        return "snap"
    if "/.var/app/org.mozilla.firefox/" in value:
        return "flatpak"
    return "native"


def driver_alive(state: dict) -> bool:
    try:
        if not process_alive(state["pid"]) or not process_alive(state["broker_pid"]):
            return False
        call(state, "session.status", timeout=1)
        return True
    except (KeyError, RuntimeError, ValueError):
        return False


def managed_lock_path(name: str) -> Path:
    return managed_dir(name) / ".browser-use-lock"


def process_alive(pid: object) -> bool:
    try:
        pid = int(pid)
        if sys.platform.startswith("linux"):
            try:
                if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0] == "Z":
                    return False
            except (OSError, IndexError):
                pass
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, TypeError, ValueError):
        return False


def firefox_lock_stale(profile: Path) -> bool:
    lock = profile / "lock"
    if not lock.is_symlink():
        return False
    match = re.search(r"\+(\d+)$", os.readlink(lock))
    return bool(match and not process_alive(match.group(1)))


def clear_firefox_locks(profile: Path) -> None:
    for name in PROFILE_LOCKS:
        (profile / name).unlink(missing_ok=True)


def acquire_managed_profile(name: str, session: str) -> Path:
    target = managed_dir(name)
    profile = target / "profile"
    if target.is_symlink() or not profile.is_dir() or target.resolve().parent != managed_root().resolve():
        raise RuntimeError(f"Managed Firefox profile not found or unsafe: {name}")
    lock = managed_lock_path(name)
    if lock.exists() or lock.is_symlink():
        lock_state = read_json_file(lock)
        owner = lock_state.get("session")
        state = load_state(str(owner)) if owner else {}
        if owner and state.get("profile_kind") == "managed" and state.get("profile") == name and driver_alive(state):
            raise RuntimeError(f"Managed Firefox profile is in use by session: {owner}")
        if process_alive(lock_state.get("pid")):
            raise RuntimeError(f"Managed Firefox profile is already being opened by session: {owner or 'unknown'}")
        lock.unlink(missing_ok=True)
    if profile_locked(profile) and firefox_lock_stale(profile):
        clear_firefox_locks(profile)
    if profile_locked(profile):
        raise RuntimeError(f"Managed Firefox profile is locked: {name}. Close Firefox first.")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError(f"Managed Firefox profile is already being opened: {name}") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"session": session, "pid": os.getpid()}, stream)
    return profile


def release_managed_profile(name: str | None, session: str) -> None:
    if not name:
        return
    lock = managed_lock_path(name)
    if read_json_file(lock).get("session") == session:
        lock.unlink(missing_ok=True)


def wait_for_profile_unlock(path: Path, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while profile_locked(path) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not profile_locked(path)


def clear_managed_firefox_locks(name: str) -> None:
    target = managed_dir(name)
    if target.is_symlink() or target.resolve().parent != managed_root().resolve():
        return
    profile = target / "profile"
    for lock_name in PROFILE_LOCKS:
        lock = profile / lock_name
        if lock.exists() or lock.is_symlink():
            lock.unlink(missing_ok=True)


def remove_profile_clone(value: str | None) -> None:
    if not value:
        return
    target = Path(value).resolve()
    allowed = {
        (home() / "profiles").resolve(),
        (profile_home() / "temporary").resolve(),
    }
    if target.parent in allowed:
        shutil.rmtree(target, ignore_errors=True)


def stop(session: str, state: dict) -> None:
    if not state:
        state_path(session).unlink(missing_ok=True)
        return
    if driver_alive(state):
        try:
            broker_request(state, "__stop__")
        except RuntimeError:
            pass
    if state.get("profile_kind") == "managed":
        name = state.get("profile")
        profile = managed_dir(name) / "profile"
        unlocked = wait_for_profile_unlock(profile)
        if not unlocked and firefox_lock_stale(profile):
            clear_managed_firefox_locks(name)
    if process_alive(state.get("pid")):
        try:
            os.kill(int(state["pid"]), signal.SIGTERM)
        except (KeyError, OSError, ValueError):
            pass
    if process_alive(state.get("broker_pid")):
        try:
            os.kill(int(state["broker_pid"]), signal.SIGTERM)
        except (KeyError, OSError, ValueError):
            pass
    if state.get("profile_kind") == "managed":
        release_managed_profile(state.get("profile"), session)
    else:
        remove_profile_clone(state.get("profile_clone"))
    state_path(session).unlink(missing_ok=True)


def start(session: str, profile: str | None = None) -> dict:
    current = load_state(session)
    if current and driver_alive(current):
        if profile and current.get("profile") != profile:
            raise RuntimeError(f"Session {session} is already running with another profile.")
        return current
    if current:
        stop(session, current)
    profile = resolve_profile(profile)

    firefox = find_firefox()
    if not firefox:
        raise RuntimeError("Firefox not found after PATH, registry, and standard-path discovery. Set FIREFOX_BINARY for a non-standard install.")
    if not headless_enabled() and not headed_ready():
        raise RuntimeError(
            "Firefox headed mode requires DISPLAY or WAYLAND_DISPLAY on Linux. "
            "Forward DISPLAY, XAUTHORITY, and DBUS_SESSION_BUS_ADDRESS from the desktop session, "
            "or set FIREFOX_USE_HEADLESS=true for tests."
        )

    profile_kind = None
    profile_path = None
    clone = None
    if profile:
        managed = managed_root() / profile if PROFILE_NAME.fullmatch(profile) else None
        if managed and managed.is_dir() and not managed.is_symlink():
            profile_kind = "managed"
            profile_path = acquire_managed_profile(profile, session)
        else:
            profile_kind = "native"
            clone = clone_profile(profile, session)
            profile_path = clone
    port, broker_port = free_port(), free_port()
    while broker_port == port:
        broker_port = free_port()
    broker_token = secrets.token_urlsafe(32)
    creationflags = 0
    popen_args = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_args["start_new_session"] = True
    broker = None
    try:
        browser_args = [firefox, "--no-remote", "--remote-debugging-port", str(port)]
        if headless_enabled():
            browser_args.append("-headless")
        if profile_path:
            browser_args.extend(("-profile", str(profile_path)))
        process = subprocess.Popen(
            browser_args,
            creationflags=creationflags, **popen_args,
        )
    except Exception:
        remove_profile_clone(str(clone) if clone else None)
        if profile_kind == "managed":
            release_managed_profile(profile, session)
        raise
    try:
        websocket_url = f"ws://127.0.0.1:{port}/session"
        last_error = None
        for _ in range(100):
            try:
                request(websocket_url, "session.status", timeout=0.5)
                break
            except RuntimeError as error:
                last_error = error
            if process.poll() is not None:
                raise RuntimeError("Firefox exited before WebDriver BiDi became ready")
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Firefox WebDriver BiDi did not become ready: {last_error}")

        broker = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve()), "_broker",
            "--websocket-url", websocket_url, "--port", str(broker_port), "--token", broker_token,
        ], creationflags=creationflags, **popen_args)
        broker_state = {"broker_port": broker_port, "broker_token": broker_token}
        for _ in range(100):
            try:
                value = broker_request(broker_state, "__status__", timeout=0.5)
                break
            except RuntimeError as error:
                last_error = error
            if broker.poll() is not None:
                raise RuntimeError("Firefox BiDi broker exited before becoming ready")
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Firefox BiDi broker did not become ready: {last_error}")

        session_id = value["sessionId"]
        state = {
            "pid": process.pid, "port": port, "session_id": session_id,
            "broker_pid": broker.pid, "broker_port": broker_port, "broker_token": broker_token,
            "elements": [], "origins": [], "profile": profile,
            "profile_kind": profile_kind, "profile_path": str(profile_path) if profile_path else None,
            "profile_clone": str(clone) if clone else None,
        }
        if profile_kind == "managed":
            mark_managed_profile_used(profile)
        save_state(session, state)
        return state
    except Exception:
        if broker:
            try:
                broker.terminate()
                broker.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass
        remove_profile_clone(str(clone) if clone else None)
        if profile_kind == "managed":
            release_managed_profile(profile, session)
        raise


def active(session: str, profile: str | None = None) -> dict:
    state = start(session, profile)
    try:
        call(state, "browsingContext.getTree")
        return state
    except RuntimeError:
        stop(session, state)
        return start(session, profile)


def origin(url: str) -> str | None:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in {"http", "https"} and parsed.netloc else None


def remember_origin(session: str, state: dict, url: str) -> None:
    if value := origin(url):
        if value not in state.setdefault("origins", []):
            state["origins"].append(value)
            save_state(session, state)


def top_contexts(state: dict) -> list[dict]:
    return call(state, "browsingContext.getTree").get("contexts", [])


def current_context(state: dict) -> dict:
    contexts = top_contexts(state)
    if not contexts:
        raise RuntimeError("Firefox has no open browsing context")
    selected = next((item for item in contexts if item["context"] == state.get("active_context")), contexts[0])
    state["active_context"] = selected["context"]
    return selected


def element(state: dict, index: int) -> dict:
    try:
        item = state["elements"][index]
    except (IndexError, KeyError):
        raise RuntimeError("Invalid element index; run state again.") from None
    return {"sharedId": item["id"], "context": item["context"]}


def enumerate_context(state: dict, context: dict, frame: list[str], output: list[dict], depth: int = 0) -> None:
    items = execute(state, """
const found=[];
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
const walk=root=>{for(const e of root.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"],[tabindex]:not([tabindex="-1"])'))if(visible(e))found.push({element:e,tag:e.tagName.toLowerCase(),type:e.type||'',text:(e.innerText||e.value||e.getAttribute('aria-label')||e.placeholder||'').trim().replace(/\s+/g,' ').slice(0,160)});for(const e of root.querySelectorAll('*'))if(e.shadowRoot)walk(e.shadowRoot)};
walk(document);return found.slice(0,200);
""", context=context["context"])
    for item in items:
        output.append({
            "id": item["element"]["sharedId"], "context": context["context"], "frame": frame,
            **{key: item[key] for key in ("tag", "type", "text")},
        })
    if depth >= 8:
        return
    for child in context.get("children") or []:
        try:
            enumerate_context(state, child, frame + [child["context"]], output, depth + 1)
        except RuntimeError:
            pass


def state_result(session: str, state: dict) -> dict:
    context = current_context(state)
    items: list[dict] = []
    enumerate_context(state, context, [], items)
    state["elements"] = [{"id": item["id"], "context": item["context"], "frame": item["frame"]} for item in items]
    save_state(session, state)
    url = context["url"]
    remember_origin(session, state, url)
    return {"url": url, "title": execute(state, "return document.title", context=context["context"]), "elements": [
        {"index": i, "tag": item["tag"], "type": item["type"], "text": item["text"], "frame": item["frame"]}
        for i, item in enumerate(items)
    ]}


def send_keys(state: dict, target: dict, text: str) -> None:
    execute(state, "arguments[0].focus();return null", [{"sharedId": target["sharedId"]}], target["context"])
    parts = text.split("+")
    values = [KEYS.get(part, part) for part in parts] if len(parts) > 1 else list(KEYS.get(text, text))
    if len(parts) > 1:
        actions = [{"type": "keyDown", "value": value} for value in values]
        actions.extend({"type": "keyUp", "value": value} for value in reversed(values))
    else:
        actions = [action for value in values for action in (
            {"type": "keyDown", "value": value}, {"type": "keyUp", "value": value},
        )]
    call(state, "input.performActions", {
        "context": target["context"],
        "actions": [{"type": "key", "id": "keyboard", "actions": actions}],
    })


def pointer_action(state: dict, target: dict, button: int, count: int = 1) -> None:
    execute(state, "arguments[0].scrollIntoView({block:'center',inline:'center'});return null", [{"sharedId": target["sharedId"]}], target["context"])
    actions = [{
        "type": "pointerMove", "duration": 0,
        "origin": {"type": "element", "element": {"sharedId": target["sharedId"]}}, "x": 0, "y": 0,
    }]
    for _ in range(count):
        actions += [{"type": "pointerDown", "button": button}, {"type": "pointerUp", "button": button}]
    call(state, "input.performActions", {"context": target["context"], "actions": [{
        "type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": actions,
    }]})


def cookie_value(value: dict) -> str:
    if value.get("type") == "base64":
        return base64.b64decode(value.get("value", "")).decode("utf-8", errors="replace")
    return value.get("value", "")


def cookie_result(cookie: dict) -> dict:
    output = {key: cookie[key] for key in ("name", "domain", "path", "secure", "httpOnly") if key in cookie}
    output["value"] = cookie_value(cookie["value"])
    if "expiry" in cookie:
        output["expiry"] = cookie["expiry"]
    if cookie.get("sameSite") != "default":
        output["sameSite"] = cookie["sameSite"].title()
    return output


def cookie_partition(state: dict) -> dict:
    return {"type": "context", "context": current_context(state)["context"]}


def cookie_path_matches(request_path: str, cookie_path: str) -> bool:
    return request_path == cookie_path or (
        request_path.startswith(cookie_path) and (cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/"))
    )


def cookies_for_url(state: dict, url: str | None = None) -> list[dict]:
    url = url or current_context(state)["url"]
    parsed = urlparse(url)
    host, path = (parsed.hostname or "").casefold(), parsed.path or "/"
    cookies = call(state, "storage.getCookies", {"partition": cookie_partition(state)})["cookies"]
    return [cookie_result(cookie) for cookie in cookies if (
        host and (host == cookie["domain"].lstrip(".").casefold() or host.endswith("." + cookie["domain"].lstrip(".").casefold()))
        and cookie_path_matches(path, cookie["path"])
        and (not cookie["secure"] or parsed.scheme == "https")
    )]


def set_cookie(state: dict, cookie: dict) -> None:
    clean = {key: cookie[key] for key in ("name", "domain", "path", "secure", "httpOnly", "expiry") if key in cookie}
    clean["value"] = {"type": "string", "value": str(cookie.get("value", ""))}
    if same_site := cookie.get("sameSite"):
        if same_site.casefold() != "default":
            clean["sameSite"] = same_site.casefold()
    call(state, "storage.setCookie", {"cookie": clean, "partition": cookie_partition(state)})


def clear_cookies_for_url(state: dict, url: str) -> None:
    for cookie in cookies_for_url(state, url):
        call(state, "storage.deleteCookies", {
            "filter": {key: cookie[key] for key in ("name", "domain", "path")},
            "partition": cookie_partition(state),
        })


def all_session_cookies(state: dict) -> list[dict]:
    current_url = current_context(state)["url"]
    origins = state.get("origins") or ([origin(current_url)] if origin(current_url) else [])
    cookies: dict[tuple, dict] = {}
    for value in origins:
        for cookie in cookies_for_url(state, value):
            exported = dict(cookie)
            exported["_origin"] = value
            cookies[(cookie.get("name"), cookie.get("domain"), cookie.get("path"))] = exported
    return list(cookies.values())


def import_cookies(state: dict, cookies: list[dict]) -> int:
    count = 0
    for cookie in cookies:
        if not str(cookie.get("domain", "")).lstrip("."):
            continue
        set_cookie(state, cookie)
        count += 1
    return count


def version(command: str | None) -> str | None:
    if not command:
        return None
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=5)
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def emit(value, json_output: bool, text: str | None = None) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False))
    elif text is not None:
        print(text)
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif value is not None:
        print(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=os.environ.get("FIREFOX_USE_SESSION", "default"))
    parser.add_argument("--profile")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--headed", action="store_true", help="Compatibility flag; Firefox is headed by default")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "state", "back", "sessions", "doctor", "selftest"):
        commands.add_parser(name)
    close = commands.add_parser("close"); close.add_argument("--all", action="store_true")
    profile = commands.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_action", required=True)
    profile_commands.add_parser("list")
    profile_create = profile_commands.add_parser("create")
    profile_create.add_argument("name")
    profile_create.add_argument("--from", dest="source")
    profile_delete = profile_commands.add_parser("delete")
    profile_delete.add_argument("name")
    for name, argument in {"open":"url", "click":"index", "type":"text", "keys":"text", "eval":"code", "screenshot":"path", "switch":"index"}.items():
        sub = commands.add_parser(name); sub.add_argument(argument, type=int if argument == "index" else str)
    close_tab = commands.add_parser("close-tab"); close_tab.add_argument("index", type=int, nargs="?")
    for name in ("input", "select", "upload"):
        sub = commands.add_parser(name); sub.add_argument("index", type=int); sub.add_argument("value")
    for name in ("hover", "dblclick", "rightclick"):
        sub = commands.add_parser(name); sub.add_argument("index", type=int)
    scroll = commands.add_parser("scroll"); scroll.add_argument("direction", choices=("up", "down")); scroll.add_argument("--amount", type=int, default=600)
    get = commands.add_parser("get"); get.add_argument("kind", choices=("title", "url", "html", "text", "value", "attributes", "bbox")); get.add_argument("index", type=int, nargs="?"); get.add_argument("--selector")
    wait = commands.add_parser("wait"); wait.add_argument("kind", choices=("text", "selector")); wait.add_argument("target"); wait.add_argument("--state", choices=("visible", "hidden", "attached", "detached"), default="visible"); wait.add_argument("--timeout", type=int, default=5000)
    cookies = commands.add_parser("cookies"); cookie_commands = cookies.add_subparsers(dest="cookie_command", required=True)
    cookie_get = cookie_commands.add_parser("get"); cookie_get.add_argument("--url")
    cookie_set = cookie_commands.add_parser("set"); cookie_set.add_argument("name"); cookie_set.add_argument("value"); cookie_set.add_argument("--domain"); cookie_set.add_argument("--path", default="/"); cookie_set.add_argument("--secure", action="store_true"); cookie_set.add_argument("--http-only", action="store_true"); cookie_set.add_argument("--same-site", choices=("Strict", "Lax", "None")); cookie_set.add_argument("--expires", type=int)
    cookie_clear = cookie_commands.add_parser("clear"); cookie_clear.add_argument("--url")
    cookie_export = cookie_commands.add_parser("export"); cookie_export.add_argument("path")
    cookie_import = cookie_commands.add_parser("import"); cookie_import.add_argument("path")
    return parser


def selftest() -> dict:
    assert safe_session("a/b") == "a_b"
    assert origin("https://example.com/a") == "https://example.com"
    assert KEYS["Enter"] == "\ue007"
    assert local_value({"sharedId": "node-1"}) == {"sharedId": "node-1"}
    assert remote_value({"type": "object", "value": [["ok", {"type": "boolean", "value": True}]]}) == {"ok": True}
    assert cookie_result({
        "name": "a", "value": {"type": "string", "value": "b"}, "domain": "example.com",
        "path": "/", "secure": False, "httpOnly": True, "sameSite": "lax",
    })["sameSite"] == "Lax"
    assert cookie_path_matches("/docs/page", "/docs") and not cookie_path_matches("/docs-old", "/docs")
    parser = build_parser()
    assert parser.parse_args(["--session", "x", "open", "about:blank"]).session == "x"
    created = parser.parse_args(["profile", "create", "work", "--from", "default-release"])
    assert created.profile_action == "create" and created.source == "default-release"
    assert validate_profile_name("work.1") == "work.1"
    if sys.platform.startswith("linux"):
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        time.sleep(0.1)
        assert not process_alive(zombie.pid)
        zombie.wait()
    try:
        validate_profile_name("../unsafe")
    except RuntimeError:
        pass
    else:
        raise AssertionError("unsafe profile name accepted")
    previous = {key: os.environ.get(key) for key in ("FIREFOX_BINARY", "FIREFOX_STATE_HOME", "BROWSER_USE_HOME", "FIREFOX_PROFILE_HOME", "FIREFOX_PROFILES_INI")}
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "firefox"
            binary.touch()
            source = root / "native"
            source.mkdir()
            (source / "marker.txt").write_text("native", encoding="utf-8")
            ini = root / "profiles.ini"
            ini.write_text("[Profile0]\nName=default-release\nIsRelative=0\nPath=" + str(source) + "\nDefault=1\n", encoding="utf-8")
            os.environ.update({
                "FIREFOX_BINARY": str(binary),
                "FIREFOX_STATE_HOME": str(root / "state"),
                "FIREFOX_PROFILE_HOME": str(root / "profiles"),
                "FIREFOX_PROFILES_INI": str(ini),
            })
            assert find_firefox() == str(binary)
            assert home() == root / "state"
            created_profile = create_managed_profile("work", "default-release")
            assert Path(created_profile["path"], "marker.txt").read_text(encoding="utf-8") == "native"
            assert resolve_profile(None) == "automation"
            create_managed_profile("social", None)
            assert resolve_profile(None) == "automation"
            mark_managed_profile_used("work")
            assert resolve_profile(None) == "automation"
            assert resolve_profile("social") == "social"
            assert delete_managed_profile("social") == {"deleted": "social"}
            assert delete_managed_profile("work") == {"deleted": "work"}
            assert resolve_profile(None) == "automation"
            assert managed_profiles()[0]["name"] == "automation"
            assert source.joinpath("marker.txt").read_text(encoding="utf-8") == "native"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "original").mkdir()
            (root / "work").mkdir()
            groups = root / "Profile Groups"
            groups.mkdir()
            ini = root / "profiles.ini"
            ini.write_text("[Profile0]\nName=default\nIsRelative=1\nPath=work\nDefault=1\nStoreID=test\n", encoding="utf-8")
            with sqlite3.connect(groups / "test.sqlite") as connection:
                connection.execute("CREATE TABLE Profiles (id INTEGER PRIMARY KEY, path TEXT, name TEXT)")
                connection.executemany("INSERT INTO Profiles (path, name) VALUES (?, ?)", [
                    ("original", "Original profile"), ("work", "Work"),
                ])
            os.environ["FIREFOX_PROFILES_INI"] = str(ini)
            assert [item["name"] for item in firefox_profiles()] == ["Original profile", "Work"]
            assert native_profile("Work")["path"] == str(root / "work")
            assert native_profile("default")["name"] == "Work"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {"ok": True}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "_broker":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--websocket-url", required=True)
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--token", required=True)
        args = parser.parse_args(sys.argv[2:])
        asyncio.run(run_broker(args.websocket_url, args.port, args.token)); return
    args = build_parser().parse_args()
    if args.command == "selftest":
        emit(selftest(), args.json, "ok"); return
    if args.command == "profile":
        if args.profile_action == "list":
            emit(firefox_profiles() + managed_profiles(), args.json)
        elif args.profile_action == "create":
            result = create_managed_profile(args.name, args.source)
            emit(result, args.json, f"Created managed Firefox profile: {args.name}")
        elif args.profile_action == "delete":
            result = delete_managed_profile(args.name)
            emit(result, args.json, f"Deleted managed Firefox profile: {args.name}")
        return
    if args.command == "doctor":
        firefox = find_firefox()
        gui_ready = headed_ready()
        result = {
            "ok": bool(firefox and (headless_enabled() or gui_ready)),
            "firefox": firefox, "firefoxVersion": version(firefox),
            "protocol": "WebDriver BiDi", "transport": "WebSocket",
            "installation": firefox_installation(), "profileHome": str(profile_home()),
            "nativeProfiles": len(firefox_profiles()), "managedProfiles": len(managed_profiles()),
            "headless": headless_enabled(), "headedReady": gui_ready,
        }
        emit(result, args.json)
        if not firefox:
            if args.json:
                raise SystemExit(1)
            raise RuntimeError("Firefox is required.")
        if not headless_enabled() and not gui_ready:
            if args.json:
                raise SystemExit(1)
            raise RuntimeError("Firefox headed mode requires DISPLAY or WAYLAND_DISPLAY on Linux.")
        return
    if args.command == "sessions":
        sessions = []
        for path in sorted(home().glob("*.json")):
            state = load_state(path.stem)
            sessions.append({
                "name": path.stem, "running": driver_alive(state),
                "profile": state.get("profile"), "profileKind": state.get("profile_kind"),
            })
        emit(sessions, args.json); return
    if args.command == "close":
        names = [path.stem for path in home().glob("*.json")] if args.all else [args.session]
        for name in names: stop(name, load_state(name))
        emit({"closed": names}, args.json, f"Closed: {', '.join(names) if names else 'none'}"); return

    state = active(args.session, args.profile)
    if args.command == "start":
        emit({"browser": "firefox", "session": args.session}, args.json, "firefox"); return
    if args.command == "open":
        context = current_context(state)
        result = call(state, "browsingContext.navigate", {"context": context["context"], "url": args.url, "wait": "complete"})
        remember_origin(args.session, state, result["url"]); emit({"url": result["url"]}, args.json, result["url"])
    elif args.command == "state":
        result = state_result(args.session, state)
        lines = [f"URL: {result['url']}", f"Title: {result['title']}"] + [f"[{e['index']}] <{e['tag']}{' type='+e['type'] if e['type'] else ''}> {e['text']}" for e in result["elements"]]
        emit(result, args.json, "\n".join(lines))
    elif args.command == "back":
        context = current_context(state); call(state, "browsingContext.traverseHistory", {"context": context["context"], "delta": -1}); emit({"url": current_context(state)["url"]}, args.json)
    elif args.command == "scroll":
        amount = args.amount if args.direction == "down" else -args.amount; execute(state, "window.scrollBy(0,arguments[0])", [amount]); emit({"scrolled": amount}, args.json)
    elif args.command == "click":
        pointer_action(state, element(state, args.index), 0); emit({"clicked": args.index}, args.json)
    elif args.command == "input":
        target = element(state, args.index); send_keys(state, target, ("Meta" if sys.platform == "darwin" else "Control") + "+a"); send_keys(state, target, "Backspace"); send_keys(state, target, args.value); emit({"input": args.index}, args.json)
    elif args.command in {"type", "keys"}:
        context = current_context(state); target = execute(state, "return document.activeElement", context=context["context"]); target["context"] = context["context"]; send_keys(state, target, args.text); emit({args.command: args.text}, args.json)
    elif args.command == "select":
        target = element(state, args.index); selected = execute(state, "const e=arguments[0],q=arguments[1],o=[...e.options].find(x=>x.value===q||x.text===q);if(!o)throw Error('option not found');e.value=o.value;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return o.text", [{"sharedId": target["sharedId"]}, args.value], target["context"]); emit({"selected": selected}, args.json, selected)
    elif args.command == "upload":
        path = Path(args.value).resolve()
        if not path.is_file(): raise RuntimeError(f"Upload file not found: {path}")
        target = element(state, args.index); call(state, "input.setFiles", {"context": target["context"], "element": {"sharedId": target["sharedId"]}, "files": [str(path)]}); emit({"uploaded": str(path)}, args.json)
    elif args.command in {"hover", "dblclick", "rightclick"}:
        count = 0 if args.command == "hover" else (2 if args.command == "dblclick" else 1); pointer_action(state, element(state, args.index), 2 if args.command == "rightclick" else 0, count); emit({args.command: args.index}, args.json)
    elif args.command == "switch":
        contexts = top_contexts(state); selected = contexts[args.index]; call(state, "browsingContext.activate", {"context": selected["context"]}); state["active_context"] = selected["context"]; state["elements"] = []; save_state(args.session, state); emit({"tab": args.index, "url": selected["url"]}, args.json)
    elif args.command == "close-tab":
        contexts = top_contexts(state); selected = contexts[args.index] if args.index is not None else current_context(state); call(state, "browsingContext.close", {"context": selected["context"]}); remaining = [] if len(contexts) == 1 else top_contexts(state)
        if remaining: call(state, "browsingContext.activate", {"context": remaining[0]["context"]}); state["active_context"] = remaining[0]["context"]; state["elements"] = []; save_state(args.session, state)
        emit({"remainingTabs": len(remaining)}, args.json)
    elif args.command == "eval":
        emit(execute(state, "return eval(arguments[0])", [args.code]), args.json)
    elif args.command == "screenshot":
        path = Path(args.path); data = call(state, "browsingContext.captureScreenshot", {"context": current_context(state)["context"], "origin": "viewport"})["data"]; path.write_bytes(base64.b64decode(data)); emit({"path": str(path)}, args.json, str(path))
    elif args.command == "get":
        context = current_context(state)
        if args.kind == "url": result = context["url"]
        elif args.kind == "title": result = execute(state, "return document.title", context=context["context"])
        elif args.kind == "html": result = execute(state, "const e=arguments[0]?document.querySelector(arguments[0]):document.documentElement;return e?e.outerHTML:null", [args.selector])
        else:
            if args.index is None: raise RuntimeError(f"get {args.kind} requires an index")
            target = element(state, args.index)
            scripts = {"text":"return arguments[0].innerText||arguments[0].textContent||''", "value":"return arguments[0].value", "attributes":"return Object.fromEntries([...arguments[0].attributes].map(a=>[a.name,a.value]))", "bbox":"const r=arguments[0].getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height}"}
            result = execute(state, scripts[args.kind], [{"sharedId": target["sharedId"]}], target["context"])
        emit(result, args.json)
    elif args.command == "wait":
        deadline = time.monotonic() + args.timeout / 1000
        if args.kind == "text": script = "return !!document.body&&document.body.innerText.includes(arguments[0])"
        else: script = "const e=document.querySelector(arguments[0]);if(arguments[1]==='attached')return!!e;if(arguments[1]==='detached')return!e;const v=!!e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none';return arguments[1]==='visible'?v:!v"
        while time.monotonic() < deadline:
            if execute(state, script, [args.target, args.state]): break
            time.sleep(0.2)
        else: raise RuntimeError(f"Timed out waiting for {args.kind}: {args.target}")
        emit({"found": args.target}, args.json)
    elif args.command == "cookies":
        if args.cookie_command == "get": result = cookies_for_url(state, args.url); emit(result, args.json)
        elif args.cookie_command == "set":
            domain = args.domain or urlparse(current_context(state)["url"]).hostname
            if not domain: raise RuntimeError("Cookie domain is required outside an http(s) page.")
            cookie = {"name": args.name, "value": args.value, "domain": domain, "path": args.path, "secure": args.secure, "httpOnly": args.http_only}
            for key, value in (("sameSite", args.same_site), ("expiry", args.expires)):
                if value is not None: cookie[key] = value
            set_cookie(state, cookie); emit({"set": args.name}, args.json)
        elif args.cookie_command == "clear":
            current_url = current_context(state)["url"]; cleared = [args.url] if args.url else (state.get("origins", []) or [current_url])
            for value in cleared: clear_cookies_for_url(state, value)
            emit({"cleared": cleared}, args.json)
        elif args.cookie_command == "export":
            cookies = all_session_cookies(state); Path(args.path).write_text(json.dumps(cookies, indent=2), encoding="utf-8"); emit({"path": args.path, "count": len(cookies)}, args.json)
        elif args.cookie_command == "import":
            cookies = json.loads(Path(args.path).read_text(encoding="utf-8")); count = import_cookies(state, cookies); emit({"imported": count}, args.json)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, IndexError, OSError, ValueError, json.JSONDecodeError) as error:
        if os.environ.get("FIREFOX_USE_DEBUG") == "1":
            raise
        json_mode = "--json" in sys.argv
        if json_mode: print(json.dumps({"error": str(error)}, ensure_ascii=False))
        else: print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
