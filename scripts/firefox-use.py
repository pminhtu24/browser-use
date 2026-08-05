#!/usr/bin/env python3
"""Deterministic Firefox automation through WebDriver BiDi."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp
from aiohttp import web

PROFILE_LOCKS = ("parent.lock", ".parentlock", "lock")
KEYS = {
    "Null": "\ue000", "Backspace": "\ue003", "Tab": "\ue004", "Enter": "\ue007",
    "Shift": "\ue008", "Control": "\ue009", "Alt": "\ue00a", "Escape": "\ue00c",
    "ArrowLeft": "\ue012", "ArrowUp": "\ue013", "ArrowRight": "\ue014",
    "ArrowDown": "\ue015", "Delete": "\ue017", "Meta": "\ue03d",
}


def home() -> Path:
    return secure_directory(Path(tempfile.gettempdir()) / "browser-use-firefox")


def secure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def state_path() -> Path:
    digest = hashlib.sha256(str(automation_profile()).encode()).hexdigest()[:16]
    return secure_directory(home() / "state") / f"{digest}.json"


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    write_private_json(state_path(), state)


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

        async def shutdown() -> None:
            try:
                await send("browser.close", {})
            except Exception:
                pass
            finally:
                stopped.set()

        if os.name != "nt":
            loop = asyncio.get_running_loop()
            for signal_name in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signal_name, lambda: asyncio.create_task(shutdown()))
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


def headless_enabled() -> bool:
    return os.environ.get("FIREFOX_USE_HEADLESS", "").lower() in {"1", "true", "yes"}


def headed_ready() -> bool:
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def profile_home() -> Path:
    installation = firefox_installation()
    if configured := os.environ.get("FIREFOX_PROFILE_HOME"):
        root = Path(configured).expanduser().resolve()
        if installation == "snap":
            allowed = (Path.home() / "snap" / "firefox" / "common").resolve()
            try:
                root.relative_to(allowed)
            except ValueError:
                raise RuntimeError(
                    f"Firefox Snap cannot use FIREFOX_PROFILE_HOME={root}. "
                    f"Use a directory inside {allowed}, or unset FIREFOX_PROFILE_HOME."
                ) from None
    elif installation == "snap":
        root = Path.home() / "snap" / "firefox" / "common" / ".browser-use-profiles"
    elif installation == "flatpak":
        root = Path.home() / ".var" / "app" / "org.mozilla.firefox" / ".browser-use-profiles"
    else:
        root = Path(tempfile.gettempdir()) / "browser-use-firefox" / "profiles"
    return root


def automation_root() -> Path:
    return profile_home() / "managed" / "automation"


def automation_profile() -> Path:
    return automation_root() / "profile"


def runtime_path() -> Path:
    return automation_root() / "runtime.json"


def ensure_private_directory(path: Path) -> Path:
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path, 0o700)
    return path


def ensure_automation_profile() -> Path:
    ensure_private_directory(profile_home())
    root = ensure_private_directory(automation_root())
    profile = ensure_private_directory(root / "profile")
    if not (root / "meta.json").exists():
        write_private_json(root / "meta.json", {"name": "automation", "createdAt": int(time.time())})
    return profile


def profile_locked(path: Path) -> bool:
    names = ("lock",) if sys.platform.startswith("linux") else PROFILE_LOCKS
    return any((path / name).exists() or (path / name).is_symlink() for name in names)


def read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


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


def wait_for_profile_unlock(path: Path, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while profile_locked(path) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not profile_locked(path)


def is_snap_launcher(binary: str) -> bool:
    normalized = binary.replace("\\", "/")
    if "/snap/" in normalized or normalized == "/snap/bin/firefox":
        return True
    try:
        launcher = Path(binary).resolve()
        content = Path(binary).read_text(encoding="utf-8", errors="ignore")[:8192]
        return "/snap/" in str(launcher) or "snap/bin/firefox" in content or "exec snap" in content
    except OSError:
        return False


def firefox_installation() -> str:
    binary = str(find_firefox() or "").replace("\\", "/")
    if is_snap_launcher(binary):
        return "snap"
    if "/flatpak/" in binary or "/.var/app/org.mozilla.firefox/" in binary:
        return "flatpak"
    return "native"


def driver_alive(state: dict) -> bool:
    try:
        browser_pid = state.get("browser_pid", state.get("pid"))
        if not process_alive(browser_pid) or not process_alive(state["broker_pid"]):
            return False
        call(state, "session.status", timeout=1)
        return True
    except (KeyError, RuntimeError, ValueError):
        return False


def firefox_lock_pid(profile: Path) -> int | None:
    lock = profile / "lock"
    if not lock.is_symlink():
        return None
    try:
        match = re.search(r"\+(\d+)$", os.readlink(lock))
        return int(match.group(1)) if match else None
    except OSError:
        return None


def firefox_lock_stale(profile: Path) -> bool:
    pid = firefox_lock_pid(profile)
    return pid is not None and not process_alive(pid)


def clear_firefox_locks(profile: Path) -> None:
    pid = firefox_lock_pid(profile)
    if pid is not None and process_alive(pid):
        raise RuntimeError(f"Refusing to remove Firefox native lock while PID {pid} is alive.")
    for name in PROFILE_LOCKS:
        (profile / name).unlink(missing_ok=True)


def process_command(pid: object) -> str:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""
    if os.name == "nt":
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CommandLine",
        ]
    else:
        command = ["ps", "-p", str(pid), "-o", "command="]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def verified_browser_process(pid: object, profile: Path) -> bool:
    if not process_alive(pid):
        return False
    command = process_command(pid)
    expected = str(profile.resolve())
    if expected not in command or "remote-debugging-port" not in command:
        return False
    lock_pid = firefox_lock_pid(profile)
    return lock_pid in (None, int(pid))


def terminate_browser(pid: object, profile: Path) -> None:
    if not verified_browser_process(pid, profile):
        raise RuntimeError(f"Firefox PID {pid} could not be verified for profile {profile}; refusing to terminate it.")
    pid = int(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError as error:
        raise RuntimeError(
            f"Firefox PID {pid} belongs to automation but the OS denied termination. "
            "Run the command outside the sandbox or close that Firefox process, then retry."
        ) from error
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_alive(pid) and os.name != "nt":
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
    if process_alive(pid):
        raise RuntimeError(f"Firefox PID {pid} did not exit; native profile lock was preserved.")
    if profile_locked(profile):
        clear_firefox_locks(profile)


async def _close_remote_browser(port: int) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
        async with client.ws_connect(f"ws://127.0.0.1:{port}/session", max_msg_size=0) as websocket:
            for command_id, method, params in (
                (1, "session.new", {"capabilities": {"alwaysMatch": {"browserName": "firefox"}}}),
                (2, "browser.close", {}),
            ):
                await websocket.send_json({"id": command_id, "method": method, "params": params})
                while True:
                    message = await websocket.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        if method == "browser.close":
                            return
                        raise RuntimeError("Firefox closed the recovery BiDi connection")
                    result = json.loads(message.data)
                    if result.get("id") != command_id:
                        continue
                    if result.get("type") == "error":
                        raise RuntimeError(result.get("message") or result.get("error") or "BiDi recovery failed")
                    break


def close_remote_browser(state: dict, profile: Path) -> bool:
    if not state.get("port"):
        return False
    try:
        asyncio.run(_close_remote_browser(int(state["port"])))
        return wait_for_profile_unlock(profile)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, ValueError):
        return False


@contextmanager
def command_mutex(timeout: float = 60):
    digest = hashlib.sha256(str(automation_profile()).encode()).hexdigest()[:16]
    path = secure_directory(home() / "locks") / f"{digest}.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "createdAt": int(time.time())}, stream)
            break
        except FileExistsError:
            owner = read_json_file(path).get("pid")
            if owner and not process_alive(owner):
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting {timeout:g} seconds for Firefox command mutex (owner PID {owner or 'unknown'}).")
            time.sleep(0.1)
    try:
        yield
    finally:
        if read_json_file(path).get("pid") == os.getpid():
            path.unlink(missing_ok=True)


def runtime_state() -> dict:
    return read_json_file(runtime_path())


def cache_from_runtime(runtime: dict) -> dict:
    return {**runtime, "elements": [], "origins": runtime.get("origins", [])}


def recover_orphan(profile: Path, state: dict) -> None:
    candidates = [state.get("browser_pid"), state.get("pid"), firefox_lock_pid(profile)]
    pid = next((int(value) for value in candidates if value and process_alive(value)), None)
    if pid is not None and state.get("broker_pid") and not process_alive(state["broker_pid"]):
        deadline = time.monotonic() + 5
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not process_alive(pid):
            pid = None
    if pid is not None:
        if not close_remote_browser(state, profile):
            terminate_browser(pid, profile)
    elif profile_locked(profile):
        recorded_pid = state.get("browser_pid", state.get("pid"))
        if firefox_lock_stale(profile) or (recorded_pid and not process_alive(recorded_pid)):
            clear_firefox_locks(profile)
        else:
            raise RuntimeError(f"Firefox profile is locked but its process cannot be verified: {profile}")
    broker_pid = state.get("broker_pid")
    if process_alive(broker_pid):
        try:
            os.kill(int(broker_pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    runtime_path().unlink(missing_ok=True)
    state_path().unlink(missing_ok=True)


def stop(state: dict | None = None) -> None:
    profile = automation_profile()
    state = state or load_state() or runtime_state()
    if state and process_alive(state.get("broker_pid")):
        try:
            broker_request(state, "__stop__")
        except RuntimeError:
            pass
    wait_for_profile_unlock(profile)
    browser_pid = state.get("browser_pid", state.get("pid")) if state else firefox_lock_pid(profile)
    if process_alive(browser_pid):
        if not close_remote_browser(state, profile):
            terminate_browser(browser_pid, profile)
    broker_pid = state.get("broker_pid") if state else None
    if process_alive(broker_pid):
        try:
            os.kill(int(broker_pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    if profile_locked(profile) and firefox_lock_stale(profile):
        clear_firefox_locks(profile)
    runtime_path().unlink(missing_ok=True)
    state_path().unlink(missing_ok=True)


def start() -> dict:
    profile = automation_profile()
    current = load_state()
    if current and driver_alive(current):
        return current
    runtime = runtime_state()
    if runtime and driver_alive(runtime):
        current = cache_from_runtime(runtime)
        save_state(current)
        return current
    stale = runtime or current
    if stale or profile_locked(profile):
        recover_orphan(profile, stale)
    profile = ensure_automation_profile()

    firefox = find_firefox()
    if not firefox:
        raise RuntimeError("Firefox not found after PATH, registry, and standard-path discovery. Set FIREFOX_BINARY for a non-standard install.")
    if not headless_enabled() and not headed_ready():
        raise RuntimeError(
            "Firefox headed mode requires DISPLAY or WAYLAND_DISPLAY on Linux. "
            "Forward DISPLAY, XAUTHORITY, and DBUS_SESSION_BUS_ADDRESS from the desktop session, "
            "or set FIREFOX_USE_HEADLESS=true for tests."
        )
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
    browser_args = [firefox, "--no-remote", "--remote-debugging-port", str(port)]
    if headless_enabled():
        browser_args.append("-headless")
    browser_args.extend(("-profile", str(profile)))
    process = subprocess.Popen(browser_args, creationflags=creationflags, **popen_args)
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
        browser_pid = firefox_lock_pid(profile) or process.pid
        runtime = {
            "browser_pid": browser_pid, "launcher_pid": process.pid, "port": port,
            "session_id": value["sessionId"], "broker_pid": broker.pid,
            "broker_port": broker_port, "broker_token": broker_token,
            "profile_path": str(profile), "started_at": int(time.time()),
        }
        write_private_json(runtime_path(), runtime)
        state = cache_from_runtime(runtime)
        save_state(state)
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
        if profile_locked(profile) and firefox_lock_stale(profile):
            clear_firefox_locks(profile)
        raise


def active() -> dict:
    state = start()
    try:
        call(state, "browsingContext.getTree")
        return state
    except RuntimeError:
        stop(state)
        return start()


def origin(url: str) -> str | None:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in {"http", "https"} and parsed.netloc else None


def remember_origin(state: dict, url: str) -> None:
    if value := origin(url):
        if value not in state.setdefault("origins", []):
            state["origins"].append(value)
            save_state(state)


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


def state_result(state: dict) -> dict:
    context = current_context(state)
    items: list[dict] = []
    enumerate_context(state, context, [], items)
    state["elements"] = [{"id": item["id"], "context": item["context"], "frame": item["frame"]} for item in items]
    save_state(state)
    url = context["url"]
    remember_origin(state, url)
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
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--headed", action="store_true", help="Compatibility flag; Firefox is headed by default")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "state", "back", "close", "doctor", "selftest"):
        commands.add_parser(name)
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
    commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices
    assert not {"sessions", "profile"} & commands.keys()
    assert "session" not in {action.dest for action in parser._actions}
    assert "profile" not in {action.dest for action in parser._actions}
    assert "all" not in {action.dest for action in commands["close"]._actions}
    if sys.platform.startswith("linux"):
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        time.sleep(0.1)
        assert not process_alive(zombie.pid)
        zombie.wait()
    previous = {key: os.environ.get(key) for key in (
        "FIREFOX_BINARY", "FIREFOX_PROFILE_HOME", "FIREFOX_STATE_HOME", "BROWSER_USE_HOME", "FIREFOX_USE_SESSION",
    )}
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "firefox"
            binary.write_text("#!/bin/sh\nexec /snap/bin/firefox \"$@\"\n", encoding="utf-8")
            os.environ.update({
                "FIREFOX_BINARY": str(binary),
                "FIREFOX_PROFILE_HOME": str(root / "profiles"),
                "FIREFOX_STATE_HOME": str(root / "ignored-state"),
                "BROWSER_USE_HOME": str(root / "ignored-home"),
                "FIREFOX_USE_SESSION": "ignored-session",
            })
            assert find_firefox() == str(binary)
            assert home() == Path(tempfile.gettempdir()) / "browser-use-firefox"
            assert firefox_installation() == "snap"
            try:
                profile_home()
            except RuntimeError as error:
                assert "Firefox Snap cannot use FIREFOX_PROFILE_HOME" in str(error)
            else:
                raise AssertionError("Firefox Snap accepted a profile outside its sandbox")
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            assert firefox_installation() == "native"
            profile = ensure_automation_profile()
            if sys.platform.startswith("linux"):
                (profile / ".parentlock").touch()
                assert not profile_locked(profile)
            runtime = {"browser_pid": 1, "broker_pid": 2, "broker_port": 3, "broker_token": "x", "origins": ["https://example.com"]}
            write_private_json(runtime_path(), runtime)
            assert runtime_path().stat().st_mode & 0o777 == 0o600
            assert cache_from_runtime(runtime)["elements"] == []
            with command_mutex():
                pass
            digest = hashlib.sha256(str(profile).encode()).hexdigest()[:16]
            mutex = home() / "locks" / f"{digest}.lock"
            write_private_json(mutex, {"pid": os.getpid()})
            try:
                with command_mutex(timeout=0.1):
                    raise AssertionError("living mutex was ignored")
            except RuntimeError:
                pass
            mutex.unlink()
            write_private_json(mutex, {"pid": 999999999})
            with command_mutex():
                assert read_json_file(mutex)["pid"] == os.getpid()
            assert not mutex.exists()
            if sys.platform.startswith("linux"):
                process = subprocess.Popen([
                    sys.executable, "-c", "import time; time.sleep(30)",
                    "--remote-debugging-port", "1", "-profile", str(profile),
                ])
                try:
                    (profile / "lock").symlink_to(f"localhost:+{process.pid}")
                    assert verified_browser_process(process.pid, profile)
                    assert not verified_browser_process(process.pid, root / "wrong")
                    terminate_browser(process.pid, profile)
                    assert not profile_locked(profile)
                finally:
                    if process_alive(process.pid):
                        process.terminate()
                    process.wait(timeout=5)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {"ok": True}


def run_command(args) -> None:
    if args.command == "doctor":
        firefox = find_firefox()
        gui_ready = headed_ready()
        state = load_state() or runtime_state()
        browser_pid = state.get("browser_pid", state.get("pid")) or firefox_lock_pid(automation_profile())
        running = process_alive(browser_pid)
        broker_alive = driver_alive(state)
        orphaned = running and not broker_alive
        result = {
            "ok": bool(firefox and (headless_enabled() or gui_ready)),
            "firefox": firefox, "firefoxVersion": version(firefox),
            "protocol": "WebDriver BiDi", "transport": "WebSocket",
            "installation": firefox_installation(), "profilePath": str(automation_profile()),
            "running": running, "brokerAlive": broker_alive, "orphaned": orphaned,
            "browserPid": browser_pid,
            "headless": headless_enabled(), "headedReady": gui_ready,
        }
        if orphaned:
            result["repair"] = "Run any Firefox command to verify and recover the orphan, or run close."
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
    if args.command == "close":
        stop()
        emit({"closed": "automation"}, args.json, "Closed: automation"); return

    state = active()
    if args.command == "start":
        emit({"browser": "firefox", "profile": "automation", "browserPid": state["browser_pid"]}, args.json, "firefox"); return
    if args.command == "open":
        context = current_context(state)
        result = call(state, "browsingContext.navigate", {"context": context["context"], "url": args.url, "wait": "complete"})
        remember_origin(state, result["url"]); emit({"url": result["url"]}, args.json, result["url"])
    elif args.command == "state":
        result = state_result(state)
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
        contexts = top_contexts(state); selected = contexts[args.index]; call(state, "browsingContext.activate", {"context": selected["context"]}); state["active_context"] = selected["context"]; state["elements"] = []; save_state(state); emit({"tab": args.index, "url": selected["url"]}, args.json)
    elif args.command == "close-tab":
        contexts = top_contexts(state); selected = contexts[args.index] if args.index is not None else current_context(state); call(state, "browsingContext.close", {"context": selected["context"]}); remaining = [] if len(contexts) == 1 else top_contexts(state)
        if remaining: call(state, "browsingContext.activate", {"context": remaining[0]["context"]}); state["active_context"] = remaining[0]["context"]; state["elements"] = []; save_state(state)
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
    with command_mutex():
        run_command(args)


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
