#!/usr/bin/env python3
"""
ramcleanup — continuous, safe RAM reclaim for macOS.

Shows Activity Monitor–style memory. Prefer quitting apps (k) to free
App Memory. Cache ballooning ([r] / --auto) is capped and refused when
it would risk jetsamming Terminal.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from select import select
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
WHITE = "\033[37m"
CLEAR = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def _page_size() -> int:
    try:
        return int(subprocess.check_output(["pagesize"], text=True).strip())
    except Exception:
        return os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


PAGE_SIZE = _page_size()


def color_for_pct(pct: float) -> str:
    if pct >= 90:
        return RED
    if pct >= 75:
        return YELLOW
    return GREEN


def bar(pct: float, width: int = 32) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    c = color_for_pct(pct)
    return f"{c}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def fmt_bytes(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{sign}{n:5.1f} {unit}"
        n /= 1024.0
    return f"{sign}{n:5.1f} PB"


# ---------------------------------------------------------------------------
# Memory (Activity Monitor–compatible)
# ---------------------------------------------------------------------------

@dataclass
class MemStats:
    total: int
    free: int
    wired: int
    compressed: int
    purgeable: int
    file_backed: int
    app: int          # App Memory
    cached: int       # Cached Files (file-backed + purgeable)
    used: int         # App + Wired + Compressed  (== Activity Monitor "Memory Used")
    used_pct: float
    cached_pct: float
    pressure: str     # normal | warn | critical


def _sysctl_int(name: str) -> int:
    out = subprocess.check_output(["sysctl", "-n", name], text=True).strip()
    return int(float(out))


def _parse_vm_stat() -> dict[str, int]:
    raw = subprocess.check_output(["vm_stat"], text=True)
    pages: dict[str, int] = {}
    for line in raw.splitlines()[1:]:
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        digits = "".join(ch for ch in val if ch.isdigit())
        if digits:
            pages[key.strip()] = int(digits) * PAGE_SIZE
    return pages


def sample_memory() -> MemStats:
    pages = _parse_vm_stat()
    total = _sysctl_int("hw.memsize")

    free = pages.get("Pages free", 0)
    wired = pages.get("Pages wired down", 0)
    compressed = pages.get("Pages occupied by compressor", 0)
    purgeable = pages.get("Pages purgeable", 0)
    file_backed = pages.get("File-backed pages", 0)
    anonymous = pages.get("Anonymous pages", 0)

    # Activity Monitor formulas
    if anonymous:
        app = max(0, anonymous - purgeable)
    else:
        try:
            pageable = _sysctl_int("vm.page_pageable_internal_count") * PAGE_SIZE
            app = max(0, pageable - purgeable)
        except Exception:
            app = pages.get("Pages active", 0)

    cached = file_backed + purgeable
    used = app + wired + compressed
    used_pct = (used / total) * 100.0 if total else 0.0
    cached_pct = (cached / total) * 100.0 if total else 0.0

    if used_pct >= 92:
        pressure = "critical"
    elif used_pct >= 80:
        pressure = "warn"
    else:
        pressure = "normal"

    return MemStats(
        total=total,
        free=free,
        wired=wired,
        compressed=compressed,
        purgeable=purgeable,
        file_backed=file_backed,
        app=app,
        cached=cached,
        used=used,
        used_pct=used_pct,
        cached_pct=cached_pct,
        pressure=pressure,
    )


# Compressed pages are anonymous app memory in the compressor — not a disk
# cache. There is no purge API; they only shrink when owning processes free
# (or are quit). Ballooning while compressed is high allocates *more*
# anonymous memory and usually makes pressure worse — skip it and quit apps.
COMPRESSED_RELIEF_BYTES = 512 * 1024 * 1024  # 512 MB
COMPRESSED_RELIEF_PCT = 0.06  # or ≥6% of RAM


def compressed_needs_relief(stats: MemStats) -> bool:
    if stats.compressed >= COMPRESSED_RELIEF_BYTES:
        return True
    if stats.total and (stats.compressed / stats.total) >= COMPRESSED_RELIEF_PCT:
        return True
    return False


def compressed_pct(stats: MemStats) -> float:
    if not stats.total:
        return 0.0
    return (stats.compressed / stats.total) * 100.0


# ---------------------------------------------------------------------------
# Quitable apps (aggregated by app name — quitting closes the whole app)
# ---------------------------------------------------------------------------

# Only truly critical / session-breaking stuff. User apps in
# /System/Applications (Preview, Terminal, …) ARE quitable.
PROTECTED = {
    "kernel_task",
    "launchd",
    "WindowServer",
    "loginwindow",
    "sysmond",
    "UserEventAgent",
    "cfprefsd",
    "distnoted",
    "opendirectoryd",
    "coreservicesd",
    "runningboardd",
    "logd",
    "configd",
    "notifyd",
    "securityd",
    "trustd",
    "amfid",
    "sandboxd",
    "systemstats",
    "powerd",
    "thermalmonitord",
    "bluetoothd",
    "coreaudiod",
    "airportd",
    "Finder",
    "Dock",
    "SystemUIServer",
    "ControlCenter",
    "WiFiAgent",
    "UniversalControl",
    "System Settings",
    "System Preferences",
    "login",
    "ramcleanup.py",
    "ramcleanup",
    "smd",
}

# Core OS paths — never quit (unless it's a real .app bundle inside).
PROTECTED_PREFIXES = (
    "/System/Library/",
    "/usr/libexec/",
    "/usr/sbin/",
    "/sbin/",
    "/usr/lib/",
    "/usr/bin/login",
)

# Interactive shells — quitting them kills the Terminal tab, not a RAM hog app.
SHELL_NAMES = {
    "bash",
    "zsh",
    "sh",
    "fish",
    "dash",
    "csh",
    "tcsh",
    "ksh",
    "osascript",  # transient; AppleScript quit would relaunch weirdly
}

# Interpreters / runtimes — often the real RAM hogs (node, java, …).
# Quitable when they hold meaningful RSS; still skip tiny noise.
RUNTIME_NAMES = {
    "node",
    "nodejs",
    "deno",
    "bun",
    "python",
    "python3",
    "Python",
    "ruby",
    "perl",
    "php",
    "java",
    "jshell",
    "scala",
    "kotlin",
    "go",
    "rustc",
    "cargo",
    "dotnet",
    "mono",
    "lua",
    "luajit",
    "R",
    "swift",
    "tclsh",
    "wish",
}

# Per-process floor to join a group (helpers can be small).
MEMBER_RSS_FLOOR = 1 * 1024 * 1024  # 1 MB
# Group must total at least this to appear in the quit list.
GROUP_RSS_FLOOR = 12 * 1024 * 1024  # 12 MB
# Runtimes / bare binaries need a bit more before we offer quit.
RUNTIME_RSS_FLOOR = 24 * 1024 * 1024  # 24 MB


@dataclass
class ProcInfo:
    """One process group (may cover many helper pids)."""
    pid: int              # primary pid (largest RSS)
    rss: int              # total RSS across all related processes
    name: str
    safe: bool            # False = system-locked, never quit
    pids: list[int] = field(default_factory=list)
    proc_count: int = 1
    path: str = ""
    is_gui_app: bool = False
    is_background: bool = False  # no .app bundle — agent / runtime / helper
    locked: bool = False   # cannot be unchecked / quit
    vitality: int = 5      # 1 (safe to quit) … 10 (critical)
    vitality_label: str = "medium"


# Names that are important to a working Mac session (even if quitable).
_VITAL_HIGH = {
    "Finder", "Dock", "SystemUIServer", "ControlCenter", "NotificationCenter",
    "WindowServer", "loginwindow", "kernel_task", "launchd", "Spotlight",
    "UniversalControl", "WiFiAgent", "AirPlayUIAgent", "UserEventAgent",
    "Terminal", "iTerm", "iTerm2", "Warp", "Alacritty", "kitty",
    "System Settings", "System Preferences", "Activity Monitor",
    "RAM Cleanup", "RamCleanup",
}
_VITAL_SECURITY = {
    "securityd", "trustd", "amfid", "sandboxd", "syspolicyd",
    "1Password", "1Password for Safari", "Keychain Access",
    "Little Snitch", "LuLu", "BlockBlock",
}
_VITAL_DEV = {
    "Cursor", "Code", "Visual Studio Code", "Xcode", "Sublime Text",
    "JetBrains Toolbox", "IntelliJ IDEA", "PyCharm", "WebStorm",
}
_VITAL_BROWSER = {
    "Safari", "Google Chrome", "Chromium", "Firefox", "Arc", "Brave Browser",
    "Microsoft Edge", "Orion", "Vivaldi",
}
_VITAL_COMMS = {
    "Slack", "Discord", "Messages", "Mail", "Microsoft Teams", "Zoom",
    "Telegram", "WhatsApp", "Signal", "Skype", "FaceTime",
}
_VITAL_MEDIA = {
    "Music", "Spotify", "TV", "VLC", "IINA", "QuickTime Player", "Podcasts",
}


def vitality_for(name: str, path: str, *, locked: bool, is_gui: bool, is_runtime: bool) -> tuple[int, str]:
    """
    Score how vital a process group is to keep running.
    10 = critical (session/OS), 1 = fine to quit for RAM.
    """
    base = name.split(" · ", 1)[0]
    low = base.lower()

    if locked or base in PROTECTED or low in {p.lower() for p in PROTECTED}:
        return 10, "critical"
    if base in _VITAL_HIGH or low in {n.lower() for n in _VITAL_HIGH}:
        return 9, "critical"
    if base in _VITAL_SECURITY or low in {n.lower() for n in _VITAL_SECURITY}:
        return 9, "critical"
    if path.startswith(PROTECTED_PREFIXES) or path.startswith("/System/"):
        return 9, "critical"
    if base in _VITAL_DEV or low in {n.lower() for n in _VITAL_DEV}:
        return 7, "high"
    if "Helper" in name or path.endswith("Helper"):
        # App helpers — usually die with the parent; medium-low alone
        return 4, "low"
    if base in _VITAL_BROWSER or low in {n.lower() for n in _VITAL_BROWSER}:
        return 5, "medium"
    if base in _VITAL_COMMS or low in {n.lower() for n in _VITAL_COMMS}:
        return 4, "low"
    if base in _VITAL_MEDIA or low in {n.lower() for n in _VITAL_MEDIA}:
        return 3, "low"
    if is_runtime or low in {n.lower() for n in RUNTIME_NAMES}:
        return 2, "safe"
    if is_gui and ("/Applications/" in path or path.startswith("/Applications/")):
        return 5, "medium"
    if not is_gui:
        return 3, "low"
    return 5, "medium"


def _exe_from_command(command: str) -> str:
    """First path-like token from a ps command line (handles spaces in .app)."""
    command = command.strip()
    if not command:
        return ""
    # Quoted path
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 1:
            return command[1:end]
    if command.startswith("'"):
        end = command.find("'", 1)
        if end > 1:
            return command[1:end]
    # Prefer the .app bundle executable when present.
    if ".app/" in command or ".app " in command or command.endswith(".app"):
        start = command.find("/")
        if start < 0:
            start = 0
        m = command[start:]
        if ".app/" in m:
            pre, post = m.split(".app/", 1)
            bits = post.split()
            if bits:
                return pre + ".app/" + bits[0]
            return pre + ".app"
        if ".app" in m:
            return m.split(".app", 1)[0] + ".app"
    return command.split(None, 1)[0]


def _app_name_from_path(path: str) -> str:
    if ".app/" in path or path.endswith(".app"):
        for seg in path.split("/"):
            if seg.endswith(".app"):
                return seg[:-4]
    name = os.path.basename(path.rstrip("/")) or path
    # Collapse "Cursor Helper (Plugin): …" → "Cursor"
    if " Helper" in name:
        name = name.split(" Helper", 1)[0]
    # Electron / Chromium: "App Helper (GPU)" already handled; also "(Renderer)"
    for suffix in (" (GPU)", " (Renderer)", " (Plugin)", " (Service)"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    # Drop Electron-style suffixes after colon
    if ":" in name and not name.startswith("/"):
        name = name.split(":", 1)[0].strip() or name
    return name


def _group_key(name: str, path: str, command: str) -> str:
    """
    Aggregate related processes. Prefer .app name; for runtimes, try to use
    a distinguishing script/project token so one giant `node` isn't opaque —
    but still one row per runtime binary name for quit-all-of-this-runtime.
    """
    base = _app_name_from_path(path) or name
    low = base.lower()
    if low in {n.lower() for n in RUNTIME_NAMES} or low in RUNTIME_NAMES:
        # Try: node /path/to/server.js → "node · server.js"
        #      java -jar foo.jar → "java · foo.jar"
        tokens = command.split()
        label = None
        for i, tok in enumerate(tokens[1:], start=1):
            if tok.startswith("-") and tok not in {"-jar", "-cp", "-classpath"}:
                continue
            if tok in {"-jar", "-cp", "-classpath"} and i + 1 < len(tokens):
                label = os.path.basename(tokens[i + 1].rstrip("/"))
                break
            if "/" in tok or tok.endswith((".js", ".mjs", ".cjs", ".ts", ".jar", ".py", ".rb")):
                label = os.path.basename(tok.rstrip("/"))
                break
        if label:
            return f"{base} · {label}"
    return base


def _is_gui_app_path(path: str) -> bool:
    return ".app/" in path or path.endswith(".app")


def _is_protected(name: str, path: str, *, rss: int = 0) -> bool:
    base = name.split(" · ", 1)[0]
    if base in PROTECTED or base.lower() in {p.lower() for p in PROTECTED}:
        return True
    if name in PROTECTED or name.lower() in {p.lower() for p in PROTECTED}:
        return True
    # Never quit the interactive shell hosting the session.
    if base.lower() in SHELL_NAMES or name.lower() in SHELL_NAMES:
        return True
    # Our own interpreter process (matched by path ending).
    base_path = os.path.basename(path.rstrip("/"))
    if base_path in {"ramcleanup.py", "webui.py"}:
        return True
    if "ramcleanup" in path and base_path.startswith("python"):
        return True
    # Allow real apps under /System/Applications and /Applications
    if "/Applications/" in path or path.startswith("/Applications/"):
        return False
    # User LaunchAgents / Homebrew / opt / local — quitable background stuff.
    home = os.path.expanduser("~")
    if path.startswith(home + "/") or path.startswith("/Users/"):
        return False
    if path.startswith(("/opt/", "/usr/local/", "/Homebrew/", "/homebrew/")):
        return False
    # .app anywhere (including some System apps we allow via Applications)
    if _is_gui_app_path(path):
        if path.startswith("/System/") and "/Applications/" not in path:
            return True
        return False
    for prefix in PROTECTED_PREFIXES:
        if path.startswith(prefix):
            return True
    # Bare system daemons with no .app
    if path.startswith("/System/") and ".app" not in path:
        return True
    # Absolute paths under /bin /usr/bin — allow large runtimes only
    if path.startswith(("/bin/", "/usr/bin/")):
        if base.lower() in {n.lower() for n in RUNTIME_NAMES}:
            return rss < RUNTIME_RSS_FLOOR
        # Other /usr/bin utilities — only if they're huge (misbehaving)
        return rss < 64 * 1024 * 1024
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def all_processes(limit: int = 150) -> list[ProcInfo]:
    """
    Aggregate open processes by app/runtime name, largest RSS first.

    Includes system-locked groups (Finder, daemons, …) so the UI can show
    everything with a vitality score. Locked rows are never quitable.
    """
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,rss=,uid=,command="],
            text=True,
        )
    except Exception:
        return []

    groups: dict[str, list[tuple[int, int, str, str]]] = {}
    my_pid = os.getpid()
    my_uid = os.getuid()

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[1]) * 1024
            uid = int(parts[2])
        except ValueError:
            continue
        if pid == my_pid or pid <= 1:
            continue
        if uid != my_uid and rss < 64 * 1024 * 1024:
            continue
        command = parts[3]
        if "ramcleanup" in command or "webui.py" in command:
            continue
        if command.startswith(("[", "ps ")):
            continue
        path = _exe_from_command(command)
        if not path:
            continue
        name = _app_name_from_path(path)
        key = _group_key(name, path, command)
        if rss < MEMBER_RSS_FLOOR and not _is_gui_app_path(path):
            continue
        groups.setdefault(key, []).append((rss, pid, path, command))

    # Show more: lower floor so "all open" is honest; still drop tiny noise.
    show_floor = 4 * 1024 * 1024

    apps: list[ProcInfo] = []
    for name, members in groups.items():
        members.sort(reverse=True)
        total = sum(r for r, _, _, _ in members)
        if total < show_floor:
            continue
        is_gui = any(_is_gui_app_path(p) for _, _, p, _ in members)
        primary_path = members[0][2]
        base = name.split(" · ", 1)[0]
        is_runtime = base.lower() in {n.lower() for n in RUNTIME_NAMES}
        locked = (
            _is_protected(name, primary_path, rss=total)
            or _is_protected(base, primary_path, rss=total)
        )
        # Shells + session UI — listed but never quit from this tool.
        if base.lower() in SHELL_NAMES or base in {
            "Finder", "Dock", "SystemUIServer", "ControlCenter",
            "WindowServer", "loginwindow", "kernel_task", "launchd",
            "Terminal", "iTerm", "iTerm2", "Warp", "Alacritty", "kitty",
            "RAM Cleanup", "RamCleanup",
        }:
            locked = True
        vitality, vlabel = vitality_for(
            name,
            primary_path,
            locked=locked,
            is_gui=is_gui,
            is_runtime=is_runtime,
        )
        pids = [p for _, p, _, _ in members]
        apps.append(
            ProcInfo(
                pid=pids[0],
                rss=total,
                name=name,
                safe=not locked,
                pids=pids,
                proc_count=len(pids),
                path=primary_path,
                is_gui_app=is_gui,
                is_background=not is_gui,
                locked=locked,
                vitality=vitality,
                vitality_label=vlabel,
            )
        )

    apps.sort(key=lambda a: (-a.rss, -a.vitality, a.name.lower()))
    return apps[:limit]


def quitable_processes(limit: int = 60) -> list[ProcInfo]:
    """Processes safe to quit (excludes system-locked), largest first."""
    return [p for p in all_processes(limit=max(limit * 2, 80)) if p.safe][:limit]


def quit_process(proc: ProcInfo) -> str:
    """
    Quit a whole app (all related pids) without launching anything.

    We deliberately do NOT use `osascript tell application … to quit`.
    That call asks Launch Services to resolve the name and will *open*
    agents / helpers / misnamed processes — the random-app leak.
    Prefer SIGTERM (especially for GUI apps) so Cocoa can tear down cleanly;
    SIGKILL only after a longer wait, and only for leftovers.
    """
    if not proc.safe:
        return f"refused: {proc.name} is protected"

    pids = [p for p in (proc.pids or [proc.pid]) if _pid_alive(p)]
    label = (
        f"{proc.name} ({proc.proc_count} procs, {fmt_bytes(proc.rss).strip()})"
    )
    if not pids:
        return f"already gone {label}"

    # Primary pid first so the app can shut helpers down itself.
    ordered = [proc.pid] + [p for p in pids if p != proc.pid]
    for pid in ordered:
        if not _pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # GUI apps need more time; immediate SIGKILL often respawns helpers.
    time.sleep(1.1 if proc.is_gui_app else 0.55)

    stubborn = [pid for pid in pids if _pid_alive(pid)]
    if stubborn and not proc.is_gui_app:
        for pid in stubborn:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.2)
    elif stubborn and proc.is_gui_app:
        # One more TERM burst; avoid SIGKILL on .app bundles (relaunch churn).
        for pid in stubborn:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.4)

    alive = sum(1 for pid in pids if _pid_alive(pid))
    if alive == 0:
        return f"quit {label}"
    return f"quit {label} · {alive} still exiting…"


def parse_quit_selection(text: str, count: int) -> Optional[list[int]]:
    """
    Parse a comma/space-separated selection.
    Returns 1-based indices, or None if cancel/empty/invalid.
    Special: 0 means all (returns every index 1..count).
    """
    text = text.strip().lower()
    if not text or text in {"c", "cancel", "n", "no"}:
        return None

    parts = [
        p.strip()
        for p in text.replace(";", ",").replace("|", ",").replace(" ", ",").split(",")
        if p.strip()
    ]
    if not parts:
        return None
    if parts == ["0"] or "0" in parts:
        return list(range(1, count + 1)) if count else None

    indices: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        n = int(part)
        if n < 1 or n > count:
            return None
        if n not in indices:
            indices.append(n)
    return indices or None


# ---------------------------------------------------------------------------
# Cache reclaim (balloon) — only frees Cached Files, not App Memory
# ---------------------------------------------------------------------------

class Reclaimer:
    # Soft cap for background [r]/auto — stays conservative (jetsam safety).
    SOFT_CAP = 512 * 1024 * 1024
    # Hard clear must exceed Free or the VM never touches file cache.
    # Absolute ceiling; per-run size is free+cached-leave (see suggest_balloon).
    HARD_CAP = 12 * 1024 * 1024 * 1024
    # Leave almost nothing — aim for Cached Files ≈ 0 (macOS may refill).
    HARD_LEAVE = 8 * 1024 * 1024
    HARD_TARGET = 8 * 1024 * 1024  # stop when cached drops below this
    HARD_PASSES = 10

    def __init__(self) -> None:
        self.last_reclaimed: int = 0
        self.last_duration: float = 0.0
        self.total_runs: int = 0
        self.last_error: Optional[str] = None
        self._libc = None
        lib = ctypes.util.find_library("c")
        if lib:
            self._libc = ctypes.CDLL(lib, use_errno=True)

    def _pressure_relief(self) -> None:
        if self._libc is None:
            return
        try:
            self._libc.malloc_zone_pressure_relief(None, 0)
        except Exception:
            pass

    def _hard_cap_for(self, stats: MemStats) -> int:
        # Never try to balloon more than ~70% of physical RAM in one go.
        by_ram = int(stats.total * 0.70) if stats.total else self.HARD_CAP
        return max(self.SOFT_CAP, min(self.HARD_CAP, by_ram))

    def balloon(self, target_bytes: int, *, hard: bool = False, cap: Optional[int] = None) -> int:
        if cap is None:
            cap = self.HARD_CAP if hard else self.SOFT_CAP
        target_bytes = max(0, min(target_bytes, cap))
        min_size = 16 * 1024 * 1024 if hard else 32 * 1024 * 1024
        if target_bytes < min_size:
            return 0

        t0 = time.monotonic()
        chunk = 64 * 1024 * 1024
        buffers: list[bytearray] = []
        held = 0
        # Touch every page when hard-clearing so purgeable/file cache actually yields.
        stride = PAGE_SIZE if hard else PAGE_SIZE * 4

        try:
            while held + chunk <= target_bytes:
                buf = bytearray(chunk)
                for i in range(0, chunk, stride):
                    buf[i] = 1
                buffers.append(buf)
                held += chunk
            # Partial final chunk if useful
            remain = target_bytes - held
            if remain >= min_size:
                buf = bytearray(remain)
                for i in range(0, remain, stride):
                    buf[i] = 1
                buffers.append(buf)
                held += remain
        except MemoryError:
            self.last_error = "MemoryError — stopped early (safe)"
        finally:
            buffers.clear()
            self._pressure_relief()

        self.last_reclaimed = held
        self.last_duration = time.monotonic() - t0
        self.total_runs += 1
        return held

    def suggest_balloon(
        self, stats: MemStats, aggressiveness: float, *, hard: bool = False
    ) -> int:
        # Cached Files = file-backed + purgeable — both are reclaimable.
        # Hard: allocate past Free so the VM must evict file-backed pages.
        # Soft: fraction of pool, capped elsewhere — never force past Free alone.
        pool = stats.cached + stats.free
        if hard:
            leave = self.HARD_LEAVE
            usable = max(0, pool - leave)
            # Full pressure on the reclaimable pool (aggressiveness ignored for hard).
            return min(usable, self._hard_cap_for(stats))
        leave = 2 * 1024 * 1024 * 1024
        usable = max(0, pool - leave)
        frac = min(1.0, aggressiveness)
        return int(usable * frac)

    def balloon_safe(
        self,
        stats: MemStats,
        *,
        hard: bool = False,
        always: bool = False,
    ) -> tuple[bool, str]:
        """Refuse ballooning when it would likely jetsam Terminal / the host.

        always=True: proactive reclaim even when pressure is normal / used is low
        (still refuses compressor-bloated and critical+no-free cases).
        """
        # Allocating into a full compressor worsens Used (App/Compressed) and
        # can jetsam random apps — which then relaunch and look like a leak.
        if compressed_needs_relief(stats):
            return False, (
                f"compressed {fmt_bytes(stats.compressed).strip()} — "
                "balloon skipped; quit apps to release compressor"
            )

        # Hard / always: reclaim under any pressure except last-resort critical.
        if hard or always:
            if stats.pressure == "critical" and stats.free < 64 * 1024 * 1024:
                return False, "critical + almost no free — quit apps first"
            floor = self.HARD_TARGET if (hard or always) else 256 * 1024 * 1024
            if stats.cached < floor and stats.purgeable < floor // 2:
                return False, (
                    f"only {fmt_bytes(stats.cached).strip()} cached — "
                    "already near zero"
                )
            return True, "ok"

        if stats.pressure != "normal":
            return False, f"pressure is {stats.pressure} — balloon would risk jetsam"
        if stats.used_pct >= 75.0:
            return False, f"used {stats.used_pct:.0f}% — balloon unsafe; quit apps (k)"
        headroom = stats.free + stats.cached
        if headroom < 3 * 1024 * 1024 * 1024:
            return False, (
                f"only {fmt_bytes(headroom).strip()} free+cache — "
                "balloon unsafe; quit apps (k)"
            )
        if stats.cached < 256 * 1024 * 1024:
            return False, (
                f"only {fmt_bytes(stats.cached).strip()} cached — "
                "nothing useful to reclaim"
            )
        return True, "ok"


def try_purge() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["sudo", "-n", "purge"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return True, "purge ok"
        return False, "purge needs passwordless sudo"
    except Exception as e:
        return False, f"purge skipped ({e})"


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

class RawKeys:
    def __enter__(self) -> "RawKeys":
        if not sys.stdin.isatty():
            raise RuntimeError(
                "stdin is not a TTY — run directly in a terminal, not piped"
            )
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()
        return self

    def __exit__(self, *_) -> None:
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        sys.stdout.write(SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()

    def _set_cbreak(self) -> None:
        tty.setcbreak(self.fd)
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()

    def poll(self) -> Optional[str]:
        if not select([self.fd], [], [], 0)[0]:
            return None
        try:
            data = os.read(self.fd, 32)
        except OSError:
            return None
        if not data or data[0] == 0x1B:
            return None
        return data[:1].decode("utf-8", errors="ignore")

    def wait(self, timeout: float) -> Optional[str]:
        ready, _, _ = select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        return self.poll()

    def readline_prompt(self, prompt: str) -> str:
        """Temporarily restore cooked/echo mode, read one full line, return to cbreak."""
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(prompt)
        sys.stdout.flush()
        # Canonical mode: one read returns the whole line after Enter.
        # Use os.read so we don't fight Python's TextIO buffer.
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            data = b""
        self._set_cbreak()
        return data.decode("utf-8", errors="ignore").strip().strip("\r\n")


# ---------------------------------------------------------------------------
# State / UI
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    running: bool = True
    paused: bool = False
    auto: bool = False  # off by default — ballooning can jetsam Terminal
    use_purge: bool = False
    interval: float = 2.0
    threshold: float = 85.0
    aggressiveness: float = 0.35
    cooldown: float = 90.0
    last_auto: float = 0.0
    status: str = "watching"
    start: float = 0.0
    reclaim_log: list[str] = field(default_factory=list)
    tops: list[ProcInfo] = field(default_factory=list)
    quiet: bool = False  # skip terminal redraw (web / headless)


def render(stats: MemStats, state: AppState, reclaimer: Reclaimer) -> None:
    if state.quiet:
        return
    uptime = int(time.monotonic() - state.start)
    uh, rem = divmod(uptime, 3600)
    um, us = divmod(rem, 60)

    pcol = {"normal": GREEN, "warn": YELLOW, "critical": RED}[stats.pressure]
    mode = "AUTO" if state.auto and not state.paused else (
        "PAUSED" if state.paused else "MANUAL"
    )
    mode_c = YELLOW if state.paused else (CYAN if state.auto else WHITE)

    total_quit_rss = sum(p.rss for p in state.tops)

    lines = [
        f"{CLEAR}{BOLD}{CYAN} ramcleanup{RESET}  "
        f"{DIM}Activity Monitor–style · quit apps to free App Memory{RESET}",
        f"{DIM}{'─' * 62}{RESET}",
        "",
        f"  Pressure   {pcol}{BOLD}{stats.pressure.upper():<10}{RESET}  "
        f"mode {mode_c}{BOLD}{mode}{RESET}",
        f"  Memory Used  {color_for_pct(stats.used_pct)}{BOLD}{fmt_bytes(stats.used)}{RESET}  "
        f"/ {fmt_bytes(stats.total)}  ({stats.used_pct:5.1f}%)",
        f"               {bar(stats.used_pct)}",
        "",
        f"  App Memory   {fmt_bytes(stats.app)}   {DIM}← held by apps (not free){RESET}",
        f"  Wired        {fmt_bytes(stats.wired)}",
        f"  Compressed   {fmt_bytes(stats.compressed)}   "
        + (
            f"{YELLOW}← bloated — quit apps (k); balloon won't help{RESET}"
            if compressed_needs_relief(stats)
            else f"{DIM}← not purgeable; quit apps to release{RESET}"
        ),
        f"  Cached Files {fmt_bytes(stats.cached)}   {DIM}← [r] may free (if safe){RESET}",
        f"  Free         {fmt_bytes(stats.free)}",
        "",
        f"{DIM}{'─' * 62}{RESET}",
        f"  Quitable apps  {DIM}({len(state.tops)} listed, "
        f"~{fmt_bytes(total_quit_rss).strip()} RSS · press k to choose){RESET}",
    ]

    if not state.tops:
        lines.append(f"  {DIM}(none found){RESET}")
    else:
        for i, proc in enumerate(state.tops, start=1):
            bits = []
            if proc.proc_count > 1:
                bits.append(f"×{proc.proc_count}")
            if proc.is_background:
                bits.append("bg")
            extra = f"  {DIM}{' · '.join(bits)}{RESET}" if bits else ""
            lines.append(
                f"   {BOLD}{i:>2}{RESET}  {fmt_bytes(proc.rss)}  {proc.name}{extra}"
            )

    lines += [
        "",
        f"{DIM}{'─' * 62}{RESET}",
        f"  Status     {state.status}",
        f"  Cache ops  {reclaimer.total_runs}   "
        f"last balloon {fmt_bytes(reclaimer.last_reclaimed)} "
        f"in {reclaimer.last_duration:.1f}s",
        f"  Purge      {'on' if state.use_purge else 'off'}   "
        f"uptime {uh:02d}:{um:02d}:{us:02d}",
        "",
    ]

    if reclaimer.last_error:
        lines.append(f"  {YELLOW}note:{RESET} {reclaimer.last_error}")
        lines.append("")

    if state.reclaim_log:
        lines.append(f"  {DIM}recent:{RESET}")
        for entry in state.reclaim_log[-5:]:
            lines.append(f"    {DIM}{entry}{RESET}")
        lines.append("")

    if compressed_needs_relief(stats):
        lines.append(
            f"  {YELLOW}hint:{RESET} Compressed is {fmt_bytes(stats.compressed).strip()} "
            f"({compressed_pct(stats):.0f}% of RAM) — only quitting apps releases it. "
            f"Press {BOLD}k{RESET} (e.g. {BOLD}1,3,5{RESET} or {BOLD}0{RESET} for all). "
            f"[r] balloon is skipped while compressed stays high."
        )
        lines.append("")
    elif stats.cached < 512 * 1024 * 1024 and stats.app > stats.total * 0.4:
        lines.append(
            f"  {YELLOW}hint:{RESET} most of your {fmt_bytes(stats.used).strip()} "
            f"is App Memory — press {BOLD}k{RESET} then type e.g. {BOLD}1,3,5{RESET} (or 0 for all)"
        )
        lines.append("")

    lines.append(
        f"  {DIM}[k] quit apps  [r] safe cache reclaim  [a]uto-balloon  [p]urge  "
        f"[space] pause  [q]uit{RESET}"
    )
    lines.append("")

    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()


def do_cache_reclaim(
    stats: MemStats,
    state: AppState,
    reclaimer: Reclaimer,
    reason: str,
    *,
    hard: bool = False,
    always: bool = False,
) -> MemStats:
    """
    Reclaim Cached Files / purgeable pages.

    hard=True (web Clear / auto-clear): balloon past Free so the VM must
    evict file-backed pages (multi-pass). soft path keeps jetsam guards.
    always=True: proactive hard reclaim even when pressure is normal / used low.
    """
    start = stats
    before = stats
    # Proactive "always" uses the hard balloon path (drive cache toward ~0).
    aggressive = hard or always

    ok, why = reclaimer.balloon_safe(before, hard=hard, always=always)
    # Even if balloon is refused, still try purge on hard/always clear.
    held = 0
    parts: list[str] = []
    passes = 0

    if ok:
        min_size = 8 * 1024 * 1024 if aggressive else 32 * 1024 * 1024
        max_passes = reclaimer.HARD_PASSES if aggressive else 1
        target = reclaimer.HARD_TARGET if aggressive else 128 * 1024 * 1024

        while passes < max_passes:
            ok_pass, why_pass = reclaimer.balloon_safe(
                before, hard=hard, always=always
            )
            if not ok_pass:
                if passes == 0:
                    parts.append(f"balloon refused — {why_pass}")
                break

            size = reclaimer.suggest_balloon(
                before, state.aggressiveness, hard=aggressive
            )
            if not aggressive:
                # Soft: never balloon more than half of currently free pages
                size = min(size, max(0, before.free // 2))

            if size < min_size:
                if passes == 0:
                    parts.append("balloon skipped — size too small")
                break

            # Hard/always: must exceed Free or we only burn Free and leave cache.
            if aggressive and size <= before.free:
                size = min(
                    before.free + max(before.cached - reclaimer.HARD_LEAVE, 0),
                    reclaimer._hard_cap_for(before),
                )
                if size < min_size:
                    parts.append("balloon skipped — cannot exceed free")
                    break

            pass_n = passes + 1
            mode = "always" if always and not hard else ("hard" if hard else "safe")
            state.status = (
                f"{mode} cache reclaim "
                f"~{fmt_bytes(size).strip()} "
                f"(pass {pass_n}/{max_passes})…"
            )
            render(before, state, reclaimer)
            cap = (
                reclaimer._hard_cap_for(before) if aggressive else reclaimer.SOFT_CAP
            )
            got = reclaimer.balloon(size, hard=aggressive, cap=cap)
            held += got
            passes += 1
            parts.append(f"balloon×{pass_n} {fmt_bytes(got).strip()}")

            after_pass = sample_memory()
            dropped = before.cached - after_pass.cached
            # Drive Cached Files toward ~0; stop only when near target or stalled.
            if after_pass.cached <= target:
                before = after_pass
                break
            if aggressive and got > 0 and dropped < 4 * 1024 * 1024:
                # This pass barely moved cache — further balloons won't help.
                before = after_pass
                break
            before = after_pass
            # Interleave purge so later passes see less file-backed cache.
            if aggressive and pass_n < max_passes:
                ok_p, pmsg = try_purge()
                parts.append(f"purge@{pass_n} {pmsg}")
                before = sample_memory()
                if before.cached <= target:
                    break
                time.sleep(0.1)
    else:
        parts.append(f"balloon refused — {why}")
        if not aggressive:
            msg = f"{time.strftime('%H:%M:%S')} {reason}: refused — {why}"
            state.reclaim_log.append(msg)
            state.status = why
            state.last_auto = time.monotonic()
            return sample_memory()

    if aggressive or state.use_purge:
        ok_p, pmsg = try_purge()
        parts.append(pmsg)
        # Final squeeze if purge left anything reclaimable.
        after_purge = sample_memory()
        if (
            aggressive
            and after_purge.cached > reclaimer.HARD_TARGET
            and not compressed_needs_relief(after_purge)
        ):
            size = reclaimer.suggest_balloon(
                after_purge, state.aggressiveness, hard=True
            )
            if size <= after_purge.free:
                size = min(
                    after_purge.free
                    + max(after_purge.cached - reclaimer.HARD_LEAVE, 0),
                    reclaimer._hard_cap_for(after_purge),
                )
            if size >= 8 * 1024 * 1024:
                state.status = (
                    f"hard cache reclaim ~{fmt_bytes(size).strip()} (final)…"
                )
                render(after_purge, state, reclaimer)
                got = reclaimer.balloon(
                    size, hard=True, cap=reclaimer._hard_cap_for(after_purge)
                )
                held += got
                parts.append(f"balloon×final {fmt_bytes(got).strip()}")
                try_purge()

    after = sample_memory()
    d_used = start.used - after.used
    d_cached = start.cached - after.cached
    d_purge = start.purgeable - after.purgeable
    msg = (
        f"{time.strftime('%H:%M:%S')} {reason}: " + " · ".join(parts)
        + f" · used {fmt_bytes(start.used).strip()}→{fmt_bytes(after.used).strip()}"
        + f" · cached {fmt_bytes(start.cached).strip()}→{fmt_bytes(after.cached).strip()}"
    )
    if d_purge > 0:
        msg += f" · purgeable −{fmt_bytes(d_purge).strip()}"
    if d_cached > 0:
        msg += f" (cached −{fmt_bytes(d_cached).strip()})"
    elif d_used <= 0 and held == 0:
        msg += " · no drop — quit apps to free App Memory"

    state.reclaim_log.append(msg)
    state.status = f"done ({reason})"
    state.last_auto = time.monotonic()
    return after


def do_quit_indices(state: AppState, indices: list[int]) -> None:
    """Quit by 1-based indices into state.tops (quitable-only list)."""
    # Snapshot targets first — quitting changes the list.
    targets: list[ProcInfo] = []
    for idx in indices:
        if 1 <= idx <= len(state.tops):
            targets.append(state.tops[idx - 1])
    if not targets:
        state.status = "no matching apps"
        return

    results: list[str] = []
    freed_est = 0
    for proc in targets:
        state.status = f"quitting {proc.name}…"
        result = quit_process(proc)
        results.append(result)
        freed_est += proc.rss
        state.reclaim_log.append(f"{time.strftime('%H:%M:%S')} {result}")

    state.tops = quitable_processes()
    state.status = (
        f"quit {len(targets)} app(s) · ~{fmt_bytes(freed_est).strip()} RSS targeted"
    )


def prompt_quit_apps(
    keys: RawKeys, state: AppState, stats: MemStats, reclaimer: Reclaimer
) -> None:
    state.tops = quitable_processes()
    if not state.tops:
        state.status = "no quitable apps found"
        return

    render(stats, state, reclaimer)
    sys.stdout.write(
        f"\n  {BOLD}Quit which?{RESET} "
        f"{DIM}comma-separated, e.g. 1,3,5 · 0 = all · Enter = cancel{RESET}\n"
    )
    sys.stdout.flush()
    line = keys.readline_prompt("  > ")
    indices = parse_quit_selection(line, len(state.tops))
    if indices is None:
        state.status = "quit cancelled"
        return
    do_quit_indices(state, indices)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Monitor RAM and quit heavy apps on macOS. "
        "Cache ballooning is manual/off by default (avoids jetsam)."
    )
    p.add_argument("--threshold", type=float, default=85.0)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--cooldown", type=float, default=90.0)
    p.add_argument("--aggressiveness", type=float, default=0.35)
    p.add_argument(
        "--auto",
        action="store_true",
        help="Enable auto cache-balloon (off by default; can jetsam Terminal)",
    )
    p.add_argument(
        "--purge",
        action="store_true",
        help="Also try sudo -n purge after cache reclaim",
    )
    p.add_argument(
        "--web",
        action="store_true",
        help="Launch the web UI instead of the terminal UI",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web UI bind host (default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Web UI port (default: 8765)",
    )
    p.add_argument(
        "--open",
        action="store_true",
        help="Open the web UI in the default browser",
    )
    return p.parse_args()


def main() -> int:
    if sys.platform != "darwin":
        print("ramcleanup is for macOS only.", file=sys.stderr)
        return 1

    args = parse_args()
    if args.web:
        from webui import run_web

        return run_web(
            host=args.host,
            port=args.port,
            open_browser=args.open,
            use_purge=bool(args.purge),
            aggressiveness=max(0.2, min(0.5, args.aggressiveness)),
        )

    state = AppState(
        auto=bool(args.auto),
        use_purge=args.purge,
        interval=max(0.5, args.interval),
        threshold=max(50.0, min(99.0, args.threshold)),
        aggressiveness=max(0.2, min(0.5, args.aggressiveness)),
        cooldown=max(30.0, args.cooldown),
        start=time.monotonic(),
        status="watching · auto-balloon off" if not args.auto else "watching · auto on",
    )
    reclaimer = Reclaimer()
    stop = False

    def _sig(_s, _f) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        keys_ctx = RawKeys()
    except RuntimeError as e:
        print(f"ramcleanup: {e}", file=sys.stderr)
        return 1

    with keys_ctx as keys:
        while state.running and not stop:
            stats = sample_memory()
            state.tops = quitable_processes()

            if (
                state.auto
                and not state.paused
                and stats.used_pct >= state.threshold
                and stats.cached >= 256 * 1024 * 1024
                and (time.monotonic() - state.last_auto) >= state.cooldown
            ):
                stats = do_cache_reclaim(stats, state, reclaimer, "auto")

            render(stats, state, reclaimer)

            deadline = time.monotonic() + state.interval
            while state.running and not stop:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ch = keys.wait(min(remaining, 0.25))
                if ch is None:
                    continue
                ch = ch.lower()
                if ch == "q":
                    state.running = False
                    break
                if ch == " ":
                    state.paused = not state.paused
                    state.status = "paused" if state.paused else "watching"
                elif ch == "a":
                    state.auto = not state.auto
                    state.status = (
                        "auto-balloon on (may jetsam if memory is tight)"
                        if state.auto
                        else "auto-balloon off"
                    )
                elif ch == "p":
                    state.use_purge = not state.use_purge
                    state.status = f"purge {'on' if state.use_purge else 'off'}"
                elif ch == "r":
                    stats = do_cache_reclaim(stats, state, reclaimer, "manual")
                elif ch == "k":
                    prompt_quit_apps(keys, state, stats, reclaimer)
                    stats = sample_memory()
                else:
                    continue
                render(stats, state, reclaimer)

    print(
        f"{CYAN}ramcleanup{RESET} stopped. "
        f"Cache reclaim runs: {reclaimer.total_runs}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
