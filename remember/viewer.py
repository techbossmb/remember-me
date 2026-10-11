"""The map viewer as a background service: start/stop/status/restart, run-at-login, and identification
by port so it can be stopped from anywhere ("I don't know which window it's running in").

The viewer answers GET /api/ping with {"app": "remember.me", "pid", "version", "root"}. Every command finds it
through that, so it never touches some other program that happens to use the port, and a viewer left over from
an older version is replaced automatically.
"""
import glob
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORTS = range(8765, 8785)
LABEL = "me.remember.viewer"


def code_version():
    h = hashlib.sha1()
    for f in sorted(glob.glob(os.path.join(HERE, "*.py")) + [os.path.join(HERE, "viz", "template.html")]):
        try:
            with open(f, "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


def ping(port):
    """The viewer's identity on this port, or None (nothing there, or not ours)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.7) as r:
            body = r.read().decode("utf-8", "replace")
        d = json.loads(body)
        return d if d.get("app") == "remember.me" else None
    except Exception:
        return None


def _port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def find(store):
    """(port, identity) of a running remember.me viewer for this store, else (None, None)."""
    info = os.path.join(store, "viewer.json")
    tried = []
    if os.path.exists(info):
        try:
            tried.append(json.load(open(info, encoding="utf-8")).get("port"))
        except (OSError, ValueError):
            pass
    for port in tried + list(PORTS):
        if port is None:
            continue
        d = ping(port)
        if d and os.path.abspath(d.get("root", "")) == os.path.abspath(store):
            return port, d
    return None, None


def _kill(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError):
        return False


def stop(store):
    port, d = find(store)
    if not port:
        return "the memory map viewer isn't running"
    _kill(d["pid"])
    for _ in range(20):
        if not ping(port):
            break
        time.sleep(0.1)
    try:
        os.remove(os.path.join(store, "viewer.json"))
    except OSError:
        pass
    return f"stopped the memory map viewer (it was at http://127.0.0.1:{port})"


def start(store):
    """Ensure a current viewer is running; returns its port. Replaces one left over from an older version."""
    port, d = find(store)
    if port and d.get("version") == code_version():
        return port
    if port:
        stop(store)
    port = next((p for p in PORTS if _port_free(p)), None)
    if port is None:
        raise SystemExit(f"no free port in {PORTS.start}-{PORTS.stop - 1} for the map viewer")
    os.makedirs(store, exist_ok=True)
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    log = open(os.path.join(store, "viewer.log"), "a", encoding="utf-8")
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"  # no console window
    subprocess.Popen([exe, os.path.join(HERE, "serve.py"), "--root", store, "--port", str(port)], cwd=store,
                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kw)
    for _ in range(40):
        if ping(port):
            break
        time.sleep(0.1)
    with open(os.path.join(store, "viewer.json"), "w", encoding="utf-8") as f:
        json.dump({"port": port}, f)
    return port


def status(store):
    port, d = find(store)
    if not port:
        return "the memory map viewer isn't running (`remember viewer start`, or just ask for the map)"
    stale = "" if d.get("version") == code_version() else " (older version; `remember viewer restart` updates it)"
    return f"memory map viewer: http://127.0.0.1:{port}  pid {d.get('pid')}{stale}"


# ---------------------------------------------------------------- run at login

def _launchd_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _systemd_path():
    return os.path.expanduser("~/.config/systemd/user/remember-viewer.service")


def _startup_path():
    return os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup", "remember-viewer.cmd")


def install_service(store):
    """Start the viewer at login, so the link always works. Returns what was set up."""
    py, cli = sys.executable, os.path.join(HERE, "cli.py")
    env = {"REMEMBER_HOME": store}
    if sys.platform == "darwin":
        p = _launchd_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        args = "".join(f"<string>{x}</string>" for x in (py, cli, "viewer", "start"))
        envx = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in env.items())
        with open(p, "w", encoding="utf-8") as f:
            f.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    f'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                    f'<key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{args}</array>'
                    f'<key>EnvironmentVariables</key><dict>{envx}</dict><key>RunAtLoad</key><true/><key>AbandonProcessGroup</key><true/></dict></plist>\n')
        subprocess.run(["launchctl", "unload", p], capture_output=True)
        subprocess.run(["launchctl", "load", p], capture_output=True)
        return f"the viewer now starts when you log in (macOS login item: {p})"
    if os.name == "nt":
        p = _startup_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\nset REMEMBER_HOME={store}\r\n"{py}" "{cli}" viewer start\r\n')
        return f"the viewer now starts when you log in (Startup folder: {p})"
    p = _systemd_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"[Unit]\nDescription=remember.me map viewer\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                f"Environment=REMEMBER_HOME={store}\nExecStart={py} {cli} viewer start\n"
                f"ExecStop={py} {cli} viewer stop\n\n[Install]\nWantedBy=default.target\n")
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "remember-viewer.service"], capture_output=True, text=True)
    return f"the viewer now starts when you log in (systemd user service: {p})" + \
        ("" if r.returncode == 0 else f"; systemctl said: {r.stderr.strip()[:200]}")


def uninstall_service():
    done = []
    if os.path.exists(_launchd_path()):
        subprocess.run(["launchctl", "unload", _launchd_path()], capture_output=True)
        os.remove(_launchd_path())
        done.append("macOS login item")
    if os.path.exists(_systemd_path()):
        subprocess.run(["systemctl", "--user", "disable", "remember-viewer.service"], capture_output=True)
        os.remove(_systemd_path())
        done.append("systemd user service")
    if os.name == "nt" and os.path.exists(_startup_path()):
        os.remove(_startup_path())
        done.append("Startup folder entry")
    return ("removed: " + ", ".join(done)) if done else "the viewer wasn't set to start at login"
