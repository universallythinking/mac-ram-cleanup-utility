#!/usr/bin/env python3
"""
ramcleanup web UI — live memory dashboard, protect apps, auto-clear.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ramcleanup import (
    AppState,
    MemStats,
    ProcInfo,
    Reclaimer,
    all_processes,
    compressed_needs_relief,
    compressed_pct,
    do_cache_reclaim,
    fmt_bytes,
    quit_process,
    sample_memory,
    try_purge,
)


AUTO_INTERVALS = {0} | set(range(5, 61, 5))
CONFIG_PATH = Path.home() / ".ramcleanup" / "webui.json"


def _load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_config(data: dict[str, Any]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _cfg_bool(cfg: dict[str, Any], key: str, default: bool) -> bool:
    if key not in cfg:
        return default
    return bool(cfg[key])


class WebState:
    def __init__(self, use_purge: bool = False, aggressiveness: float = 0.35) -> None:
        self.lock = threading.RLock()
        cfg = _load_config()
        # Default KEEP: only names in `released` are eligible to quit.
        # Legacy `protected` = keep-list → invert into released on first load.
        if "released" in cfg and isinstance(cfg.get("released"), list):
            self.released: set[str] = {
                str(n).strip() for n in cfg["released"] if str(n).strip()
            }
            self._legacy_protected: Optional[set[str]] = None
        else:
            legacy = cfg.get("protected") or []
            self._legacy_protected = {
                str(n).strip() for n in legacy if str(n).strip()
            } if isinstance(legacy, list) else set()
            self.released = set()
        self.auto_interval: int = 0  # 0 = off, else 5..60 in steps of 5
        # What the timer clears each interval (manual Clear uses the same targets).
        self.clear_cached: bool = _cfg_bool(cfg, "clear_cached", True)
        self.clear_purgeable: bool = _cfg_bool(cfg, "clear_purgeable", True)
        self.clear_quit_apps: bool = _cfg_bool(cfg, "clear_quit_apps", False)
        # Proactive cache reclaim even when pressure is normal (don't wait).
        # Default on so Clear still drives cache down; uncheck to wait for pressure.
        self.always_reclaim_cached: bool = _cfg_bool(
            cfg, "always_reclaim_cached", True
        )
        # Back-compat: older configs used auto_quit for "quit on timer".
        if "clear_quit_apps" not in cfg and "auto_quit" in cfg:
            self.clear_quit_apps = bool(cfg["auto_quit"])
        self.auto_quit: bool = self.clear_quit_apps  # alias for payload/UI
        self.quit_on_clear: bool = True  # manual Clear follows clear_quit_apps
        self.use_purge = True  # always try purgeable reclaim on clear
        self.aggressiveness = max(0.35, aggressiveness)
        self.status = "ready · all processes default to Keep"
        self.reclaim_log: list[str] = []
        self.last_auto: float = 0.0
        self.start = time.monotonic()
        self.reclaimer = Reclaimer()
        self._clearing = False

    def _is_kept(self, proc: ProcInfo) -> bool:
        if proc.locked or not proc.safe:
            return True
        return proc.name not in self.released

    def _persist(self) -> None:
        _save_config(
            {
                "released": sorted(self.released),
                # Keep writing protected for older builds / clarity (kept names).
                "protected": [],
                "clear_cached": self.clear_cached,
                "clear_purgeable": self.clear_purgeable,
                "clear_quit_apps": self.clear_quit_apps,
                "always_reclaim_cached": self.always_reclaim_cached,
            }
        )

    def _targets_summary(self) -> str:
        parts: list[str] = []
        if self.clear_cached:
            parts.append(
                "cached (always)" if self.always_reclaim_cached else "cached"
            )
        if self.clear_purgeable:
            parts.append("purgeable")
        if self.clear_quit_apps:
            parts.append("quit apps")
        return ", ".join(parts) if parts else "nothing"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            stats = sample_memory()
            procs = all_processes(limit=150)
            # One-shot legacy migrate: old protected keep-list → released = others.
            if self._legacy_protected is not None:
                if self._legacy_protected:
                    for p in procs:
                        if p.safe and not p.locked and p.name not in self._legacy_protected:
                            self.released.add(p.name)
                self._legacy_protected = None
                self._persist()
            return self._payload(stats, procs)

    def _payload(self, stats: MemStats, procs: list[ProcInfo]) -> dict[str, Any]:
        apps = []
        kept = 0
        releasable = 0
        for p in procs:
            # Hide OS/session-critical rows — they can't be quit and clutter the list.
            if p.vitality_label == "critical" or p.vitality >= 9:
                continue
            keep = self._is_kept(p)
            if keep:
                kept += 1
            elif p.safe:
                releasable += 1
            apps.append(
                {
                    "name": p.name,
                    "pid": p.pid,
                    "rss": p.rss,
                    "rss_label": fmt_bytes(p.rss).strip(),
                    "proc_count": p.proc_count,
                    "protected": keep,  # checked = Keep
                    "kept": keep,
                    "locked": p.locked or not p.safe,
                    "can_quit": p.safe and not p.locked,
                    "is_gui_app": p.is_gui_app,
                    "is_background": p.is_background,
                    "vitality": p.vitality,
                    "vitality_label": p.vitality_label,
                }
            )
        will_quit = [a for a in apps if a["can_quit"] and not a["kept"]]
        # Activity Monitor: Memory Used = App + Wired + Compressed
        # Cached Files are separate (reclaimable) and not part of Used.
        return {
            "memory": {
                "total": stats.total,
                "total_label": fmt_bytes(stats.total).strip(),
                "used": stats.used,
                "used_label": fmt_bytes(stats.used).strip(),
                "used_pct": round(stats.used_pct, 1),
                "app": stats.app,
                "app_label": fmt_bytes(stats.app).strip(),
                "wired": stats.wired,
                "wired_label": fmt_bytes(stats.wired).strip(),
                "compressed": stats.compressed,
                "compressed_label": fmt_bytes(stats.compressed).strip(),
                "compressed_pct": round(compressed_pct(stats), 1),
                "compressed_relief": compressed_needs_relief(stats),
                "cached": stats.cached,
                "cached_label": fmt_bytes(stats.cached).strip(),
                "purgeable": stats.purgeable,
                "purgeable_label": fmt_bytes(stats.purgeable).strip(),
                "free": stats.free,
                "free_label": fmt_bytes(stats.free).strip(),
                "pressure": stats.pressure,
            },
            "apps": apps,
            "will_quit": will_quit,
            "kept_count": kept,
            "released_count": len(self.released),
            "protected": sorted(
                a["name"] for a in apps if a["kept"]
            ),
            "auto_interval": self.auto_interval,
            "auto_quit": self.clear_quit_apps,
            "clear_cached": self.clear_cached,
            "clear_purgeable": self.clear_purgeable,
            "clear_quit_apps": self.clear_quit_apps,
            "always_reclaim_cached": self.always_reclaim_cached,
            "quit_on_clear": self.clear_quit_apps,
            "status": self.status,
            "use_purge": self.use_purge,
            "cache_ops": self.reclaimer.total_runs,
            "last_balloon": fmt_bytes(self.reclaimer.last_reclaimed).strip(),
            "log": list(self.reclaim_log[-12:]),
            "clearing": self._clearing,
            "uptime": int(time.monotonic() - self.start),
        }

    def set_protected(self, names: list[str], protected: bool) -> dict[str, Any]:
        """protected=True → Keep (remove from released); False → allow quit."""
        with self.lock:
            procs = {p.name: p for p in all_processes(limit=150)}
            for name in names:
                name = (name or "").strip()
                if not name:
                    continue
                proc = procs.get(name)
                if proc is not None and (proc.locked or not proc.safe):
                    self.released.discard(name)
                    continue
                if protected:
                    self.released.discard(name)
                else:
                    self.released.add(name)
            self._persist()
            n_keep = sum(
                1 for p in procs.values() if self._is_kept(p)
            )
            n_quit = sum(
                1
                for p in procs.values()
                if p.safe and not p.locked and p.name in self.released
            )
            self.status = f"keeping {n_keep} · will quit {n_quit}"
            return self.snapshot()

    def set_auto_interval(self, seconds: int) -> dict[str, Any]:
        with self.lock:
            if seconds not in AUTO_INTERVALS:
                raise ValueError("interval must be 0 or 5–60 in steps of 5")
            self.auto_interval = seconds
            self.last_auto = time.monotonic()
            if seconds:
                self.status = (
                    f"auto-clear every {seconds}s · {self._targets_summary()}"
                )
            else:
                self.status = "auto-clear off"
            return self.snapshot()

    def set_auto_quit(self, enabled: bool) -> dict[str, Any]:
        """Back-compat wrapper — prefer set_clear_targets."""
        return self.set_clear_targets(quit_apps=bool(enabled))

    def set_clear_targets(
        self,
        *,
        cached: Optional[bool] = None,
        purgeable: Optional[bool] = None,
        quit_apps: Optional[bool] = None,
        always_reclaim_cached: Optional[bool] = None,
    ) -> dict[str, Any]:
        with self.lock:
            if cached is not None:
                self.clear_cached = bool(cached)
                # Turning off Cached Files also turns off proactive reclaim.
                if not self.clear_cached:
                    self.always_reclaim_cached = False
            if purgeable is not None:
                self.clear_purgeable = bool(purgeable)
            if quit_apps is not None:
                self.clear_quit_apps = bool(quit_apps)
                self.auto_quit = self.clear_quit_apps
            if always_reclaim_cached is not None:
                self.always_reclaim_cached = bool(always_reclaim_cached)
                if self.always_reclaim_cached:
                    self.clear_cached = True
            self._persist()
            if self.auto_interval:
                self.status = (
                    f"auto-clear every {self.auto_interval}s · "
                    f"{self._targets_summary()}"
                )
            else:
                self.status = f"clear targets · {self._targets_summary()}"
            return self.snapshot()

    def clear_ram(self, reason: str = "manual") -> dict[str, Any]:
        with self.lock:
            if self._clearing:
                self.status = "clear already in progress"
                return self.snapshot()
            want_cached = self.clear_cached
            want_purgeable = self.clear_purgeable
            want_quit = self.clear_quit_apps
            always_cached = self.always_reclaim_cached and want_cached
            # Cache reclaim covers both file-backed and purgeable pages.
            want_reclaim = want_cached or want_purgeable
            if not want_reclaim and not want_quit:
                self.status = "nothing selected to clear — check a target"
                self.last_auto = time.monotonic()
                return self.snapshot()
            self._clearing = True

        try:
            with self.lock:
                before = sample_memory()
                tops = all_processes(limit=150)
                is_auto = reason.startswith("auto")
                comp_bloated = compressed_needs_relief(before)
                # Compressed isn't purgeable — only app quit releases it.
                # Honor the Quit apps checkbox; if compressed is bloated and
                # quit isn't selected, reclaim alone won't help Used.
                quit_reason = "quit released" if want_quit else ""
                bits_plan: list[str] = []
                if want_reclaim:
                    bits_plan.append(
                        "cache always" if always_cached else "cache"
                    )
                if want_quit:
                    bits_plan.append("quit")
                self.status = (
                    f"clearing ({reason}) · compressed relief…"
                    if (want_quit and comp_bloated)
                    else f"clearing ({reason}) · {', '.join(bits_plan)}…"
                )
                reclaimer = self.reclaimer
                bridge = AppState(
                    use_purge=True,
                    aggressiveness=self.aggressiveness,
                    reclaim_log=self.reclaim_log,
                    status=self.status,
                    last_auto=self.last_auto,
                    quiet=True,
                )

            # When the compressor is bloated: quit FIRST, then light cache
            # reclaim. Hard balloon while compressed is high allocates more
            # anonymous RAM, worsens Used, and can jetsam/relaunch apps.
            targets: list[ProcInfo] = []
            freed_est = 0
            if want_quit and comp_bloated:
                targets = [
                    p for p in tops
                    if p.safe and not p.locked and not self._is_kept(p)
                ]
                for proc in targets:
                    result = quit_process(proc)
                    freed_est += proc.rss
                    with self.lock:
                        self.reclaim_log.append(
                            f"{time.strftime('%H:%M:%S')} {result}"
                        )
                # Let the compressor unwind after quits before touching cache.
                time.sleep(0.6)

            mid = sample_memory()
            if want_reclaim:
                under_pressure = mid.pressure in {"warn", "critical"}
                # Cached Files: only under pressure unless Always reclaim is on.
                run_cached = want_cached and (always_cached or under_pressure)
                if run_cached:
                    do_cache_reclaim(
                        mid,
                        bridge,
                        reclaimer,
                        reason,
                        hard=True,
                        always=always_cached,
                    )
                elif want_purgeable:
                    # Purgeable only — don't balloon the file cache.
                    before_p = mid.purgeable
                    ok_p, pmsg = try_purge()
                    after_p = sample_memory()
                    bridge.status = f"done ({reason}) · {pmsg}"
                    with self.lock:
                        self.reclaim_log.append(
                            f"{time.strftime('%H:%M:%S')} {reason}: {pmsg}"
                            f" · purgeable "
                            f"{fmt_bytes(before_p).strip()}→"
                            f"{fmt_bytes(after_p.purgeable).strip()}"
                            + (
                                " · cached left alone "
                                f"(pressure {mid.pressure}; "
                                "enable Always reclaim to clear it)"
                                if want_cached and not always_cached
                                else ""
                            )
                        )
                else:
                    bridge.status = (
                        f"done ({reason}) · cached skipped "
                        f"(pressure {mid.pressure} — enable Always reclaim)"
                    )
                    with self.lock:
                        self.reclaim_log.append(
                            f"{time.strftime('%H:%M:%S')} {reason}: "
                            f"cached skipped — pressure {mid.pressure} "
                            "(enable Always reclaim cached)"
                        )
            else:
                bridge.status = f"done ({reason}) · cache skipped"

            # If we deferred quit (cache-first path), quit now.
            if want_quit and not targets:
                targets = [
                    p for p in tops
                    if p.safe and not p.locked and not self._is_kept(p)
                ]
                for proc in targets:
                    result = quit_process(proc)
                    freed_est += proc.rss
                    with self.lock:
                        self.reclaim_log.append(
                            f"{time.strftime('%H:%M:%S')} {result}"
                        )

            after = sample_memory()
            d_comp = before.compressed - after.compressed
            d_cached = before.cached - after.cached
            d_purge = before.purgeable - after.purgeable

            with self.lock:
                self.status = bridge.status
                self.last_auto = time.monotonic()
                self.reclaim_log = list(bridge.reclaim_log)
                bits = []
                if want_reclaim:
                    bits.append(
                        f"cached {fmt_bytes(before.cached).strip()}→"
                        f"{fmt_bytes(after.cached).strip()}"
                    )
                    if want_purgeable or d_purge:
                        bits.append(
                            f"purgeable {fmt_bytes(before.purgeable).strip()}→"
                            f"{fmt_bytes(after.purgeable).strip()}"
                        )
                bits.append(
                    f"compressed {fmt_bytes(before.compressed).strip()}→"
                    f"{fmt_bytes(after.compressed).strip()}"
                )
                if d_comp > 0:
                    bits.append(f"compressed −{fmt_bytes(d_comp).strip()}")
                if d_cached > 0:
                    bits.append(f"cached −{fmt_bytes(d_cached).strip()}")
                if targets:
                    bits.insert(
                        0,
                        f"{quit_reason or 'quit'} ×{len(targets)} "
                        f"(~{fmt_bytes(freed_est).strip()} RSS)",
                    )
                    self.status = (
                        f"cleared {len(targets)} app(s) · "
                        f"compressed {fmt_bytes(before.compressed).strip()}→"
                        f"{fmt_bytes(after.compressed).strip()}"
                    )
                elif compressed_needs_relief(after) and not want_quit:
                    self.status = (
                        f"done ({reason}) · compressed still "
                        f"{fmt_bytes(after.compressed).strip()} — "
                        "enable Quit apps to release it"
                    )
                elif compressed_needs_relief(after):
                    self.status = (
                        f"done ({reason}) · compressed still "
                        f"{fmt_bytes(after.compressed).strip()} — "
                        "unprotect apps to release it"
                    )
                elif "skipped" in (bridge.status or ""):
                    self.status = bridge.status
                elif want_reclaim:
                    self.status = f"done ({reason}) · purgeable/cache reclaimed"
                else:
                    self.status = f"done ({reason})"
                if is_auto and not bits and not targets:
                    bits.append("no targets selected")
                self.reclaim_log.append(
                    f"{time.strftime('%H:%M:%S')} {reason}: " + " · ".join(bits)
                )
                return self.snapshot()
        finally:
            with self.lock:
                self._clearing = False


STATE: Optional[WebState] = None


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ramcleanup</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=IBM+Plex+Mono:wght@400;500;600&family=Syne:wght@600;700;800&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg0: #e8eef2;
    --bg1: #d4e0e8;
    --ink: #132029;
    --muted: #5a6d7a;
    --line: rgba(19, 32, 41, 0.12);
    --panel: rgba(255, 255, 255, 0.55);
    --panel-solid: #f4f8fb;
    --accent: #0d7a6f;
    --accent-deep: #095a52;
    --warn: #b45309;
    --crit: #b91c1c;
    --ok: #047857;
    --shadow: 0 18px 50px rgba(19, 32, 41, 0.08);
    --radius: 18px;
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; }
  body {
    font-family: "DM Sans", system-ui, sans-serif;
    color: var(--ink);
    background:
      radial-gradient(1200px 700px at 12% -10%, #b8d4de 0%, transparent 55%),
      radial-gradient(900px 600px at 100% 0%, #9ec5bc 0%, transparent 50%),
      linear-gradient(165deg, var(--bg0), var(--bg1));
    background-attachment: fixed;
  }

  .wrap {
    width: min(1100px, calc(100% - 2rem));
    margin: 0 auto;
    padding: 2.5rem 0 4rem;
  }

  header.hero {
    display: grid;
    gap: 1.25rem;
    margin-bottom: 2rem;
    animation: rise 0.7s ease both;
  }

  @keyframes rise {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: none; }
  }

  @keyframes pulse-dot {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.35); opacity: 0.55; }
  }

  .brand {
    font-family: Syne, sans-serif;
    font-weight: 800;
    font-size: clamp(2.6rem, 7vw, 4.2rem);
    letter-spacing: -0.04em;
    line-height: 0.95;
    margin: 0;
  }

  .tagline {
    margin: 0;
    max-width: 34rem;
    color: var(--muted);
    font-size: 1.05rem;
    line-height: 1.45;
  }

  .live {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .live i {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--ok);
    display: inline-block;
    animation: pulse-dot 1.8s ease infinite;
  }

  .grid {
    display: grid;
    grid-template-columns: 1.1fr 0.9fr;
    gap: 1.25rem;
  }
  @media (max-width: 860px) {
    .grid { grid-template-columns: 1fr; }
  }

  .panel {
    background: var(--panel);
    backdrop-filter: blur(14px);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 1.35rem 1.4rem;
    animation: rise 0.8s ease both;
  }
  .panel:nth-child(2) { animation-delay: 0.08s; }
  .panel h2 {
    font-family: Syne, sans-serif;
    font-size: 1.05rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin: 0 0 1rem;
  }

  .used-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .used-big {
    font-family: "IBM Plex Mono", monospace;
    font-weight: 600;
    font-size: clamp(2rem, 5vw, 2.8rem);
    letter-spacing: -0.03em;
  }
  .used-big small {
    font-size: 0.45em;
    color: var(--muted);
    font-weight: 400;
  }
  .pressure {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 0.35rem 0.65rem;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: rgba(255,255,255,0.5);
  }
  .pressure.normal { color: var(--ok); }
  .pressure.warn { color: var(--warn); }
  .pressure.critical { color: var(--crit); }

  .meter {
    margin-top: 1rem;
    height: 12px;
    border-radius: 999px;
    background: rgba(19, 32, 41, 0.08);
    overflow: hidden;
  }
  .meter > span {
    display: block;
    height: 100%;
    width: 0%;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--accent), #1aa391);
    transition: width 0.45s ease, background 0.3s ease;
  }
  .meter.warn > span { background: linear-gradient(90deg, #d97706, #f59e0b); }
  .meter.critical > span { background: linear-gradient(90deg, #dc2626, #f87171); }

  .used-eq {
    margin-top: 0.65rem;
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.72rem;
    color: var(--muted);
    letter-spacing: 0.02em;
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.75rem 1rem;
    margin-top: 1.25rem;
  }
  @media (max-width: 560px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
  }
  .stat {
    border-top: 1px solid var(--line);
    padding-top: 0.65rem;
  }
  .stat .k {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--muted);
  }
  .stat .v {
    font-family: "IBM Plex Mono", monospace;
    font-size: 1.05rem;
    margin-top: 0.2rem;
  }
  .stat.reclaim .k { color: var(--accent-deep); }
  .stat.hot .k { color: var(--warn); }
  .stat.hot .v { color: var(--warn); }

  .comp-banner {
    display: none;
    margin-top: 1rem;
    padding: 0.75rem 0.9rem;
    border-radius: 12px;
    border: 1px solid rgba(180, 83, 9, 0.35);
    background: rgba(180, 83, 9, 0.1);
    font-size: 0.88rem;
    line-height: 1.4;
    color: var(--ink);
  }
  .comp-banner.show { display: block; }
  .comp-banner strong {
    font-family: "IBM Plex Mono", monospace;
    font-weight: 600;
  }

  .controls {
    display: grid;
    gap: 1rem;
  }
  .field label {
    display: block;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--muted);
    margin-bottom: 0.45rem;
  }
  .field select {
    appearance: none;
    width: 100%;
    border: 1px solid var(--line);
    background: rgba(255,255,255,0.65)
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%235a6a74' d='M1.4 1.2 6 5.8l4.6-4.6 1.2 1.2L6 8.2.2 2.4z'/%3E%3C/svg%3E")
      no-repeat right 0.9rem center;
    color: var(--ink);
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.9rem;
    font-weight: 500;
    padding: 0.7rem 2.2rem 0.7rem 0.9rem;
    border-radius: 12px;
    cursor: pointer;
    transition: border-color 0.2s ease, background-color 0.2s ease;
  }
  .field select:hover { border-color: rgba(13,122,111,0.35); }
  .field select:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(13,122,111,0.15);
  }

  .targets {
    display: grid;
    gap: 0.45rem;
    padding: 0.75rem 0.85rem;
    border-radius: 12px;
    border: 1px solid var(--line);
    background: rgba(255,255,255,0.45);
  }
  .targets-label {
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--muted);
    margin-bottom: 0.15rem;
  }
  .targets-sub {
    font-size: 0.78rem;
    color: var(--muted);
    line-height: 1.35;
    margin: 0.15rem 0 0.35rem;
  }
  .target-row {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: start;
    gap: 0.65rem;
    padding: 0.35rem 0;
    cursor: pointer;
  }
  .target-row input[type="checkbox"] {
    width: 1.05rem;
    height: 1.05rem;
    margin-top: 0.1rem;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .target-row .target-title {
    font-weight: 600;
    font-size: 0.92rem;
    letter-spacing: -0.01em;
  }
  .target-row .target-hint {
    font-size: 0.75rem;
    color: var(--muted);
    line-height: 1.35;
    margin-top: 0.1rem;
  }
  .target-row.disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .target-row.disabled input { cursor: not-allowed; }
  .target-row.target-sub {
    margin-left: 1.35rem;
    padding-left: 0.65rem;
    border-left: 2px solid rgba(13, 122, 111, 0.25);
  }

  .btn-row { display: flex; gap: 0.6rem; flex-wrap: wrap; }
  button.primary, button.ghost {
    appearance: none;
    border: none;
    font-family: Syne, sans-serif;
    font-weight: 700;
    font-size: 0.95rem;
    letter-spacing: -0.01em;
    padding: 0.85rem 1.15rem;
    border-radius: 12px;
    cursor: pointer;
    transition: transform 0.15s ease, background 0.2s ease, opacity 0.2s ease;
  }
  button.primary {
    background: var(--ink);
    color: #f4f8fb;
  }
  button.primary:hover { background: var(--accent-deep); transform: translateY(-1px); }
  button.primary:disabled { opacity: 0.5; cursor: wait; transform: none; }
  button.ghost {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--line);
  }
  button.ghost:hover { color: var(--ink); border-color: rgba(19,32,41,0.25); }

  .status-line {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.8rem;
    color: var(--muted);
    min-height: 1.2em;
  }

  .process-grid {
    display: grid;
    grid-template-columns: 1.35fr 0.85fr;
    gap: 1.25rem;
    margin-top: 1.25rem;
  }
  @media (max-width: 960px) {
    .process-grid { grid-template-columns: 1fr; }
  }
  .processes-panel, .release-panel {
    animation: rise 0.9s ease both;
    animation-delay: 0.12s;
  }
  .release-panel { animation-delay: 0.18s; }
  .apps-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 0.85rem;
  }
  .apps-head p {
    margin: 0.35rem 0 0;
    color: var(--muted);
    font-size: 0.9rem;
    line-height: 1.4;
  }
  .hint-checks {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    color: var(--accent-deep);
  }

  .app-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 0.35rem;
    max-height: min(62vh, 640px);
    overflow: auto;
  }
  .app-row {
    display: grid;
    grid-template-columns: auto auto 1fr auto auto;
    align-items: center;
    gap: 0.75rem;
    padding: 0.7rem 0.85rem;
    border-radius: 12px;
    border: 1px solid transparent;
    background: rgba(255,255,255,0.35);
    transition: background 0.2s ease, border-color 0.2s ease;
  }
  .app-row:hover { background: rgba(255,255,255,0.7); }
  .app-row.protected {
    border-color: rgba(13, 122, 111, 0.35);
    background: rgba(13, 122, 111, 0.08);
  }
  .app-row.released {
    border-color: rgba(185, 28, 28, 0.28);
    background: rgba(185, 28, 28, 0.06);
  }
  .app-row.locked {
    opacity: 0.72;
  }
  .app-row input[type="checkbox"] {
    width: 1.05rem;
    height: 1.05rem;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .app-row.locked input { cursor: not-allowed; }
  .vitality {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    min-width: 4.2rem;
    text-align: center;
    padding: 0.25rem 0.4rem;
    border-radius: 8px;
    border: 1px solid var(--line);
    background: rgba(255,255,255,0.55);
  }
  .vitality.critical { color: var(--crit); border-color: rgba(185,28,28,0.35); }
  .vitality.high { color: var(--warn); border-color: rgba(180,83,9,0.35); }
  .vitality.medium { color: var(--muted); }
  .vitality.low, .vitality.safe {
    color: var(--ok);
    border-color: rgba(4,120,87,0.3);
  }
  .app-name {
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .bg-tag, .lock-tag {
    display: inline-block;
    margin-left: 0.4rem;
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    vertical-align: middle;
  }
  .bg-tag { color: var(--warn); }
  .lock-tag { color: var(--crit); }
  .app-row.background:not(.protected):not(.released) {
    background: rgba(180, 83, 9, 0.06);
  }
  .app-meta {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.72rem;
    color: var(--muted);
  }
  .app-rss {
    font-family: "IBM Plex Mono", monospace;
    font-weight: 500;
    font-size: 0.92rem;
    white-space: nowrap;
  }
  .shield {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--accent-deep);
    opacity: 0;
    transition: opacity 0.2s ease;
  }
  .app-row.protected .shield { opacity: 1; }
  .app-row.released .shield {
    opacity: 1;
    color: var(--crit);
  }

  .release-list .app-row {
    grid-template-columns: 1fr auto;
  }
  .release-empty {
    color: var(--muted);
    font-size: 0.9rem;
    line-height: 1.45;
    padding: 0.5rem 0.15rem 0.25rem;
  }
  .release-total {
    margin-top: 0.75rem;
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.8rem;
    color: var(--muted);
  }

  .log {
    margin-top: 1rem;
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.72rem;
    color: var(--muted);
    max-height: 9rem;
    overflow: auto;
    border-top: 1px solid var(--line);
    padding-top: 0.75rem;
  }
  .log div { padding: 0.15rem 0; }
  .empty {
    color: var(--muted);
    padding: 1rem 0.25rem;
  }
</style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="live"><i></i> live on localhost</div>
      <h1 class="brand">ramcleanup</h1>
      <p class="tagline">Watch memory, protect what you need, and choose what each interval (and Clear) reclaim. Compressed RAM only drops when apps quit.</p>
    </header>

    <div class="grid">
      <section class="panel" aria-label="Memory">
        <h2>Memory</h2>
        <div class="used-row">
          <div class="used-big" id="usedBig">— <small>/ —</small></div>
          <div class="pressure normal" id="pressure">—</div>
        </div>
        <div class="meter" id="meter"><span id="meterFill"></span></div>
        <div class="used-eq" id="usedEq">Used = App + Wired + Compressed</div>
        <div class="stats">
          <div class="stat"><div class="k">App Memory</div><div class="v" id="statApp">—</div></div>
          <div class="stat"><div class="k">Wired</div><div class="v" id="statWired">—</div></div>
          <div class="stat" id="statCompressedWrap"><div class="k">Compressed</div><div class="v" id="statCompressed">—</div></div>
          <div class="stat reclaim"><div class="k">Cached Files</div><div class="v" id="statCached">—</div></div>
          <div class="stat reclaim"><div class="k">Purgeable</div><div class="v" id="statPurgeable">—</div></div>
          <div class="stat"><div class="k">Free</div><div class="v" id="statFree">—</div></div>
        </div>
        <div class="comp-banner" id="compBanner" role="status">
          Compressed is high — there’s no purge for it. Uncheck low-vitality processes in <strong>All open processes</strong>, enable Quit apps, then <strong>Clear RAM</strong>.
        </div>
      </section>

      <section class="panel controls" aria-label="Controls">
        <h2>Auto-clear</h2>
        <div class="field">
          <label for="intervalSelect">Interval</label>
          <select id="intervalSelect" aria-label="Auto-clear interval">
            <option value="0">Off</option>
            <option value="5">5s</option>
            <option value="10">10s</option>
            <option value="15">15s</option>
            <option value="20">20s</option>
            <option value="25">25s</option>
            <option value="30">30s</option>
            <option value="35">35s</option>
            <option value="40">40s</option>
            <option value="45">45s</option>
            <option value="50">50s</option>
            <option value="55">55s</option>
            <option value="60">60s</option>
          </select>
        </div>
        <div class="targets" id="clearTargets">
          <div class="targets-label">Clear on each interval</div>
          <p class="targets-sub">Same targets apply to Clear RAM now. Cached Files wait for pressure unless Always reclaim is on. Wired/Free can’t be cleared; Compressed only shrinks via Quit apps.</p>
          <label class="target-row">
            <input type="checkbox" id="targetCached" checked />
            <div>
              <div class="target-title">Cached Files</div>
              <div class="target-hint">File-backed pages — cleared when pressure is warn/critical</div>
            </div>
          </label>
          <label class="target-row target-sub" id="alwaysCachedRow">
            <input type="checkbox" id="targetAlwaysCached" checked />
            <div>
              <div class="target-title">Always reclaim cached</div>
              <div class="target-hint">Don’t wait for pressure — drive Cached Files toward zero on every Clear</div>
            </div>
          </label>
          <label class="target-row">
            <input type="checkbox" id="targetPurgeable" checked />
            <div>
              <div class="target-title">Purgeable</div>
              <div class="target-hint">Memory the system marked as reclaimable</div>
            </div>
          </label>
          <label class="target-row">
            <input type="checkbox" id="targetQuitApps" />
            <div>
              <div class="target-title">Quit apps</div>
              <div class="target-hint">Quits processes you unchecked (Will quit pane) — frees App Memory and Compressed</div>
            </div>
          </label>
        </div>
        <div class="btn-row">
          <button type="button" class="primary" id="clearBtn">Clear RAM now</button>
          <button type="button" class="ghost" id="miniBtn" title="Shrink to always-on-top widget">Mini window</button>
          <button type="button" class="ghost" id="protectAllBtn">Keep all</button>
          <button type="button" class="ghost" id="protectNoneBtn">Release safe (≤4)</button>
        </div>
        <div class="status-line" id="statusLine">ready</div>
      </section>
    </div>

    <div class="process-grid">
      <section class="panel processes-panel" aria-label="All processes">
        <div class="apps-head">
          <div>
            <h2 style="margin:0">All open processes</h2>
            <p>Checked = <strong>Keep</strong> (default). Uncheck to allow quitting when Quit apps is on. Critical system/session apps are hidden.</p>
          </div>
          <div class="hint-checks" id="protectCount">0 kept</div>
        </div>
        <ul class="app-list" id="appList"></ul>
        <div class="log" id="log"></div>
      </section>

      <section class="panel release-panel" aria-label="Will quit">
        <div class="apps-head">
          <div>
            <h2 style="margin:0">Will quit</h2>
            <p>Unchecked processes. Clear with Quit apps enabled terminates these first when Compressed is high.</p>
          </div>
          <div class="hint-checks" id="releaseCount">0 released</div>
        </div>
        <ul class="app-list release-list" id="releaseList"></ul>
        <div class="release-total" id="releaseTotal"></div>
      </section>
    </div>
  </div>

<script>
(() => {
  const $ = (id) => document.getElementById(id);
  let busy = false;

  async function api(path, opts) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      ...opts,
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(t || r.statusText);
    }
    return r.json();
  }

  function render(data) {
    const m = data.memory;
    $("usedBig").innerHTML = `${m.used_label} <small>/ ${m.total_label}</small>`;
    const pressure = $("pressure");
    pressure.textContent = m.pressure;
    pressure.className = "pressure " + m.pressure;
    const meter = $("meter");
    meter.className = "meter" + (m.pressure !== "normal" ? " " + m.pressure : "");
    $("meterFill").style.width = Math.min(100, m.used_pct) + "%";
    $("statApp").textContent = m.app_label;
    $("statWired").textContent = m.wired_label;
    $("statCompressed").textContent = m.compressed_label;
    $("statCached").textContent = m.cached_label;
    $("statPurgeable").textContent = m.purgeable_label;
    $("statFree").textContent = m.free_label;
    const compHot = !!m.compressed_relief;
    $("statCompressedWrap").classList.toggle("hot", compHot);
    $("compBanner").classList.toggle("show", compHot);
    $("usedEq").textContent =
      `Used ${m.used_label} = App ${m.app_label} + Wired ${m.wired_label} + Compressed ${m.compressed_label}`
      + (compHot ? ` · compressor ${m.compressed_pct}% of RAM — quit to release` : "");
    $("statusLine").textContent = data.status;
    const kept = data.kept_count != null ? data.kept_count : (data.protected || []).length;
    const released = data.released_count != null
      ? data.released_count
      : (data.will_quit || []).length;
    $("protectCount").textContent = `${kept} kept`;
    $("releaseCount").textContent = `${released} released`;
    $("targetCached").checked = !!data.clear_cached;
    $("targetAlwaysCached").checked = !!data.always_reclaim_cached;
    $("targetPurgeable").checked = !!data.clear_purgeable;
    $("targetQuitApps").checked = !!data.clear_quit_apps;
    $("targetAlwaysCached").disabled = !data.clear_cached;
    $("alwaysCachedRow").classList.toggle("disabled", !data.clear_cached);

    $("intervalSelect").value = String(data.auto_interval);

    const list = $("appList");
    if (!data.apps.length) {
      list.innerHTML = `<li class="empty">No processes found right now.</li>`;
    } else {
      list.innerHTML = data.apps.map((app) => {
        const keptRow = !!app.kept || !!app.protected;
        const locked = !!app.locked;
        const v = app.vitality_label || "medium";
        const tags = [
          app.is_background ? '<span class="bg-tag">bg</span>' : "",
          locked ? '<span class="lock-tag">locked</span>' : "",
        ].join("");
        const cls = [
          "app-row",
          keptRow ? "protected" : "released",
          app.is_background ? "background" : "",
          locked ? "locked" : "",
        ].filter(Boolean).join(" ");
        return `
        <li class="${cls}" data-name="${escapeAttr(app.name)}" data-vitality="${app.vitality || 0}">
          <input type="checkbox" ${keptRow ? "checked" : ""} ${locked ? "disabled" : ""}
            aria-label="Keep ${escapeAttr(app.name)}" />
          <div class="vitality ${escapeAttr(v)}" title="Vitality ${app.vitality}/10">${app.vitality}/10</div>
          <div>
            <div class="app-name">${escapeHtml(app.name)}${tags}</div>
            <div class="app-meta">${escapeHtml(v)} · ${app.proc_count > 1 ? "×" + app.proc_count + " · " : ""}pid ${app.pid}</div>
          </div>
          <div class="shield">${keptRow ? "keep" : "quit"}</div>
          <div class="app-rss">${escapeHtml(app.rss_label)}</div>
        </li>`;
      }).join("");
    }

    const release = $("releaseList");
    const willQuit = data.will_quit || data.apps.filter((a) => a.can_quit && !(a.kept || a.protected));
    if (!willQuit.length) {
      release.innerHTML = `<li class="release-empty">Nothing released yet. Uncheck low-vitality rows (safe/low) to free App Memory on Clear.</li>`;
      $("releaseTotal").textContent = "";
    } else {
      let bytes = 0;
      release.innerHTML = willQuit.map((app) => {
        bytes += app.rss || 0;
        return `
        <li class="app-row released" data-name="${escapeAttr(app.name)}">
          <div>
            <div class="app-name">${escapeHtml(app.name)}</div>
            <div class="app-meta">vitality ${app.vitality}/10 · ${escapeHtml(app.vitality_label || "")}</div>
          </div>
          <div class="app-rss">${escapeHtml(app.rss_label)}</div>
        </li>`;
      }).join("");
      $("releaseTotal").textContent = `~${formatBytes(bytes)} RSS targeted`;
    }

    $("log").innerHTML = (data.log || []).slice().reverse().map((e) =>
      `<div>${escapeHtml(e)}</div>`
    ).join("") || `<div>No activity yet.</div>`;

    $("clearBtn").disabled = !!data.clearing || busy;
  }

  function formatBytes(n) {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let x = Math.max(0, Number(n) || 0);
    let i = 0;
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
    return `${x.toFixed(1)} ${u[i]}`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    })[c]);
  }
  function escapeAttr(s) {
    return escapeHtml(s).replace(/\n/g, " ");
  }

  $("appList").addEventListener("change", async (ev) => {
    const input = ev.target;
    if (input.type !== "checkbox") return;
    const row = input.closest(".app-row");
    if (!row) return;
    const name = row.dataset.name;
    try {
      const data = await api("/api/protect", {
        method: "POST",
        body: JSON.stringify({ names: [name], protected: input.checked }),
      });
      render(data);
    } catch (e) {
      input.checked = !input.checked;
      $("statusLine").textContent = String(e.message || e);
    }
  });

  $("intervalSelect").addEventListener("change", async (ev) => {
    const seconds = Number(ev.target.value);
    try {
      const data = await api("/api/auto", {
        method: "POST",
        body: JSON.stringify({ interval: seconds }),
      });
      render(data);
    } catch (e) {
      $("statusLine").textContent = String(e.message || e);
    }
  });

  async function pushTargets(fromEl) {
    const body = {
      cached: $("targetCached").checked,
      purgeable: $("targetPurgeable").checked,
      quit_apps: $("targetQuitApps").checked,
      always_reclaim_cached: $("targetAlwaysCached").checked,
    };
    try {
      const data = await api("/api/clear-targets", {
        method: "POST",
        body: JSON.stringify(body),
      });
      render(data);
    } catch (e) {
      if (fromEl) fromEl.checked = !fromEl.checked;
      $("statusLine").textContent = String(e.message || e);
    }
  }
  ["targetCached", "targetAlwaysCached", "targetPurgeable", "targetQuitApps"].forEach((id) => {
    $(id).addEventListener("change", (ev) => pushTargets(ev.target));
  });

  $("clearBtn").addEventListener("click", async () => {
    busy = true;
    $("clearBtn").disabled = true;
    $("statusLine").textContent = "clearing…";
    try {
      const data = await api("/api/clear", { method: "POST", body: "{}" });
      render(data);
    } catch (e) {
      $("statusLine").textContent = String(e.message || e);
    } finally {
      busy = false;
    }
  });

  $("miniBtn").addEventListener("click", async () => {
    $("statusLine").textContent = "opening mini window…";
    try {
      // Prefer native bridge when running inside RamCleanup.app
      if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ramcleanup) {
        window.webkit.messageHandlers.ramcleanup.postMessage("mini");
        return;
      }
      await api("/api/mini", { method: "POST", body: "{}" });
      $("statusLine").textContent = "mini requested — check top-right";
    } catch (e) {
      $("statusLine").textContent = String(e.message || e);
    }
  });

  async function bulkProtect(protected, { maxVitality } = {}) {
    const rows = [...document.querySelectorAll("#appList .app-row")].filter((r) => {
      const input = r.querySelector('input[type="checkbox"]');
      if (!input || input.disabled) return false;
      if (maxVitality != null) {
        return Number(r.dataset.vitality || 99) <= maxVitality;
      }
      return true;
    });
    const names = rows.map((r) => r.dataset.name).filter(Boolean);
    if (!names.length) return;
    try {
      const data = await api("/api/protect", {
        method: "POST",
        body: JSON.stringify({ names, protected }),
      });
      render(data);
    } catch (e) {
      $("statusLine").textContent = String(e.message || e);
    }
  }
  $("protectAllBtn").addEventListener("click", () => bulkProtect(true));
  // Release only low-vitality / safe-to-quit rows (score ≤ 4)
  $("protectNoneBtn").addEventListener("click", () =>
    bulkProtect(false, { maxVitality: 4 })
  );

  async function tick() {
    try {
      const data = await api("/api/status");
      if (!busy) render(data);
    } catch (_) { /* keep last frame */ }
  }

  tick();
  setInterval(tick, 2000);
})();
</script>
</body>
</html>
"""


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # Quiet default access spam; status is visible in the UI.
        return

    def do_GET(self) -> None:
        assert STATE is not None
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/status":
            _json_response(self, 200, STATE.snapshot())
            return
        _json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        assert STATE is not None
        path = urlparse(self.path).path
        try:
            data = _read_json(self)
        except Exception as e:
            _json_response(self, 400, {"error": str(e)})
            return

        try:
            if path == "/api/protect":
                names = data.get("names") or []
                if isinstance(names, str):
                    names = [names]
                protected = bool(data.get("protected", True))
                _json_response(self, 200, STATE.set_protected(list(names), protected))
                return
            if path == "/api/auto":
                interval = int(data.get("interval", 0))
                _json_response(self, 200, STATE.set_auto_interval(interval))
                return
            if path == "/api/auto-quit":
                enabled = bool(data.get("enabled", False))
                _json_response(self, 200, STATE.set_auto_quit(enabled))
                return
            if path == "/api/clear-targets":
                kwargs = {}
                if "cached" in data:
                    kwargs["cached"] = bool(data["cached"])
                if "purgeable" in data:
                    kwargs["purgeable"] = bool(data["purgeable"])
                if "quit_apps" in data:
                    kwargs["quit_apps"] = bool(data["quit_apps"])
                if "always_reclaim_cached" in data:
                    kwargs["always_reclaim_cached"] = bool(
                        data["always_reclaim_cached"]
                    )
                # Accept legacy auto_quit key too
                if "quit_apps" not in kwargs and "enabled" in data:
                    kwargs["quit_apps"] = bool(data["enabled"])
                _json_response(self, 200, STATE.set_clear_targets(**kwargs))
                return
            if path == "/api/clear":
                _json_response(self, 200, STATE.clear_ram("manual"))
                return
            if path == "/api/mini":
                # Native app polls ~/.ramcleanup/cmd_mini and shows the widget.
                cmd = Path.home() / ".ramcleanup" / "cmd_mini"
                try:
                    cmd.parent.mkdir(parents=True, exist_ok=True)
                    cmd.write_text("mini\n", encoding="utf-8")
                except OSError as e:
                    _json_response(self, 500, {"error": str(e)})
                    return
                with STATE.lock:
                    STATE.status = "mini window requested"
                _json_response(self, 200, STATE.snapshot())
                return
        except ValueError as e:
            _json_response(self, 400, {"error": str(e)})
            return
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})
            return

        _json_response(self, 404, {"error": "not found"})


def _auto_loop(stop: threading.Event) -> None:
    assert STATE is not None
    while not stop.wait(1.0):
        with STATE.lock:
            interval = STATE.auto_interval
            last = STATE.last_auto
            clearing = STATE._clearing
        if not interval or clearing:
            continue
        if (time.monotonic() - last) < interval:
            continue
        try:
            STATE.clear_ram("auto")
        except Exception as e:
            with STATE.lock:
                STATE.status = f"auto-clear error: {e}"
                STATE.last_auto = time.monotonic()


def run_web(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    use_purge: bool = False,
    aggressiveness: float = 0.35,
) -> int:
    global STATE
    STATE = WebState(use_purge=use_purge, aggressiveness=aggressiveness)
    stop = threading.Event()
    worker = threading.Thread(target=_auto_loop, args=(stop,), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"ramcleanup web UI → {url}")
    print(
        "Clear / interval: uses the Cached Files, Purgeable, and Quit apps "
        "checkboxes. When Quit apps is on and Compressed is high, quits "
        "unchecked apps first. Checked apps are never quit. Ctrl+C to stop."
    )

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nramcleanup web UI stopped.")
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_web(open_browser=True))
