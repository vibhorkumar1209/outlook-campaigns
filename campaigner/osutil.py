"""Where macOS and Windows differ: file locks, keeping the machine awake, and starting the dashboard at login."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from xml.sax.saxutils import escape

WINDOWS = sys.platform == "win32"
MACHINE = "PC" if WINDOWS else "Mac"
ROOT = Path(__file__).resolve().parent.parent
LABEL = "com.outlook-campaigns.dashboard"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
TASK = "OutlookCampaigns"  # Windows Task Scheduler name


# ---------- file locks (one run / one Chrome profile at a time across processes) ----------

def open_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a+")  # never truncate: Windows can refuse to truncate a file another process has locked


def try_lock(f) -> bool:
    if WINDOWS:
        import msvcrt

        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def unlock(f) -> None:
    if WINDOWS:
        import msvcrt

        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(f, fcntl.LOCK_UN)


# ---------- keep awake ----------

class KeepAwake:
    """Stops idle sleep while a campaign is active, so sequences keep running with the screen off.
    macOS: a `caffeinate -i -w <pid>` child (ends with this process). Windows: SetThreadExecutionState held by a
    dedicated thread (the state is per thread and clears when that thread ends). Neither stops lid-close sleep."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def on(self) -> bool:
        if WINDOWS:
            return bool(self._thread and self._thread.is_alive())
        return bool(self.proc and self.proc.poll() is None)

    def update(self, want: bool) -> None:
        if want and not self.on:
            if WINDOWS:
                self._stop = threading.Event()
                self._thread = threading.Thread(target=_hold_awake, args=(self._stop,), daemon=True)
                self._thread.start()
            elif shutil.which("caffeinate"):
                self.proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not want and self.on:
            if WINDOWS:
                self._stop.set()
                self._thread.join(5)
                self._thread = None
            else:
                self.proc.terminate()
                self.proc = None


def _hold_awake(stop: threading.Event) -> None:
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    k = ctypes.windll.kernel32
    k.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        stop.wait()
    finally:
        k.SetThreadExecutionState(ES_CONTINUOUS)


# ---------- start at login ----------

def python_exe(windowless: bool = False) -> str:
    exe = Path(sys.executable)
    if windowless and WINDOWS and exe.with_name("pythonw.exe").exists():
        return str(exe.with_name("pythonw.exe"))
    return str(exe)


def supervised() -> bool:
    """True when something restarts the dashboard after it exits: launchd's KeepAlive or `oc supervise`."""
    return os.environ.get("OC_SUPERVISED") == "1" or os.environ.get("XPC_SERVICE_NAME") == LABEL


def service_installed() -> bool:
    if WINDOWS:
        return subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True).returncode == 0
    return PLIST.exists()


def install_service(serve_args: list[str], log: Path) -> None:
    if WINDOWS:
        _install_task(serve_args, log)
        return
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / "oc"), "serve", *serve_args],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"OC_SUPERVISED": "1"},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)


def uninstall_service() -> bool:
    if WINDOWS:
        if not service_installed():
            return False
        subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], check=True, capture_output=True)
        return True
    if not PLIST.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    PLIST.unlink()
    return True


def _install_task(serve_args: list[str], log: Path) -> None:
    """A per-user logon task running `supervise` with pythonw (no console window). The supervisor restarts the
    dashboard whenever it exits, like launchd's KeepAlive: Task Scheduler's own restart-on-failure ignores exit codes."""
    args = subprocess.list2cmdline(["-m", "campaigner", "supervise", "--log", str(log), *serve_args])
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>outlook-campaigns dashboard and scheduler</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(_win_user())}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(_win_user())}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(python_exe(windowless=True))}</Command>
    <Arguments>{escape(args)}</Arguments>
    <WorkingDirectory>{escape(str(ROOT))}</WorkingDirectory>
  </Exec></Actions>
</Task>"""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "task.xml"
        f.write_text(xml, encoding="utf-16")
        subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
        subprocess.run(["schtasks", "/Create", "/TN", TASK, "/XML", str(f), "/F"], check=True, capture_output=True)
    subprocess.run(["schtasks", "/Run", "/TN", TASK], check=True, capture_output=True)


def _win_user() -> str:
    dom, user = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{dom}\\{user}" if dom else user


def exit_with_parent(pid: int) -> None:
    """Windows: end this process when the supervisor dies (e.g. `schtasks /End`), so no orphan keeps the port."""
    if not WINDOWS:
        return

    def watch():
        import ctypes

        SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
        k = ctypes.windll.kernel32
        h = k.OpenProcess(SYNCHRONIZE, False, pid)
        if h:
            k.WaitForSingleObject(h, INFINITE)
            os._exit(0)

    threading.Thread(target=watch, daemon=True).start()
