"""Updates from GitHub: `git pull` the checkout, reinstall requirements if they changed, then restart when idle."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from .osutil import ROOT


def _git(*args: str, timeout: int = 60) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def status(fetch: bool = True) -> dict:
    """{ok, branch, version, behind, changes: [subjects], dirty} or {ok: False, error}."""
    try:
        if fetch:
            _git("fetch", "--quiet", timeout=30)
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        version = _git("log", "-1", "--format=%h · %cd", "--date=format:%b %d, %Y")
        try:
            changes = _git("log", "--format=%s", "HEAD..@{u}").splitlines()
        except RuntimeError:
            changes = []  # no upstream (local branch)
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
        return {"ok": True, "branch": branch, "version": version, "behind": len(changes), "changes": changes[:20],
                "dirty": dirty}
    except FileNotFoundError:
        return {"ok": False, "error": "Git isn't installed. Install it from git-scm.com to get updates."}
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}


def apply() -> str:
    """Fast-forward to the latest code. Raises RuntimeError with git's message if that isn't possible
    (local edits in the way, or a diverged branch). Returns a short summary."""
    before = _git("rev-parse", "HEAD")
    _git("pull", "--ff-only", "--quiet", timeout=120)
    after = _git("rev-parse", "HEAD")
    if before == after:
        return "Already up to date."
    n = len(_git("rev-list", f"{before}..{after}").splitlines())
    if "requirements.txt" in _git("diff", "--name-only", before, after).splitlines():
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("Code updated, but installing requirements failed:\n" + r.stderr[-800:])
    return f"Updated: {n} new change(s)."


def exit_when_idle(runner, wait: float = 5) -> None:
    """Exit once no run is in progress and the LinkedIn bot's Chrome is closed, holding both locks so nothing new
    starts. launchd (macOS) or the supervisor (Windows) starts the dashboard again on the new code. Never mid-send:
    exiting during a send could send that email twice."""
    from . import linkedin_bot
    from .runner import tick_lock

    def go():
        runner.log("restart requested: waiting for the current run and the LinkedIn bot to finish")
        while True:
            if runner._lock.acquire(blocking=False):
                if linkedin_bot.PROFILE_LOCK.acquire(blocking=False):
                    with tick_lock() as ok:
                        if ok:
                            runner.log("restarting now")
                            time.sleep(0.5)
                            os._exit(0)
                    linkedin_bot.PROFILE_LOCK.release()
                runner._lock.release()
            time.sleep(wait)

    threading.Thread(target=go, daemon=True).start()
