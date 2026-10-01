"""Best-effort adapter for Codex's local JSONL events, not an official quota API."""
import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("notify")
MAX_LINE = 4 * 1024 * 1024


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


class CodexWatcher:
    def __init__(self, app):
        self.app = app
        settings = app.cfg["codex"]
        default = "~/.codex/sessions"
        self.root = Path(os.environ["CODEX_HOME"]) / "sessions" if settings["sessions_dir"] == default and os.environ.get("CODEX_HOME") else Path(settings["sessions_dir"]).expanduser()
        self.files = {}
        self.boot = time.time()

    def usage(self, limits, observed):
        if not isinstance(limits, dict) or not observed or time.time() - observed > 86400:
            return
        limit_id = str(limits.get("limit_id") or "codex")
        state = self.app.store.get_state("codex_usage", {})
        previous = state.get(limit_id, {})
        if observed < previous.get("observed_at", 0):
            return
        windows = {}
        for name in ("primary", "secondary"):
            window = limits.get(name)
            if not isinstance(window, dict):
                continue
            used, resets = window.get("used_percent"), window.get("resets_at")
            if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
                continue
            if type(resets) not in (int, float) or not math.isfinite(resets) or resets <= time.time():
                continue
            remaining = round(100 - used, 1)
            minutes = window.get("window_minutes")
            label = f"{minutes}-minute" if minutes else name
            windows[name] = {"remaining_percent": remaining, "resets_at": resets, "window_minutes": minutes}
            crossed = [n for n in self.app.cfg["codex"]["remaining_thresholds"] if remaining <= n]
            if crossed:
                # First observation below several thresholds emits just the most urgent one.
                threshold = min(crossed)
                alert_state = f"usage-alert:{limit_id}:{name}:{resets}"
                if threshold >= self.app.store.get_state(alert_state, 101):
                    continue
                reset_label = datetime.fromtimestamp(resets).astimezone().strftime("%b %d, %H:%M %Z")
                if self.app.cfg["codex"].get("auto_notify"):
                    self.app.notify({"title": f"Codex usage: {remaining:g}% remaining", "source": "codex", "event": "usage_low",
                                     "message": f"{limit_id} {label} allowance has {remaining:g}% remaining. Resets {reset_label}.",
                                     "priority": 4 if remaining > 5 else 5,
                                     "dedupe_key": f"usage:{limit_id}:{name}:{resets}:{threshold}"})
                self.app.store.set_state(alert_state, threshold)
        state[limit_id] = {"observed_at": observed, "windows": windows}
        self.app.store.set_state("codex_usage", state)

    def process(self, row, info, offset):
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if row.get("type") != "event_msg":
            return
        if kind == "token_count":
            self.usage(payload.get("rate_limits"), timestamp(row.get("timestamp")))
        elif kind == "task_complete" and not info["subagent"] and self.app.cfg["codex"].get("auto_notify"):
            observed = timestamp(row.get("timestamp"))
            if not observed or observed < self.boot - 300:
                return
            title = "Codex finished" + (f" — {info['project']}" if info["project"] else "")
            summary = payload.get("last_agent_message") if self.app.cfg["codex"]["include_summary"] else None
            self.app.notify({"title": title[:150], "message": str(summary or "Your Codex turn has finished.")[:2500],
                             "source": "codex", "event": "done", "dedupe_key": f"codex:{info['id']}:{payload.get('turn_id') or offset}"[:300]})

    def discover(self, path, stat):
        with path.open("rb") as file:
            first = file.readline(MAX_LINE)
            try:
                meta = json.loads(first).get("payload", {})
            except (ValueError, AttributeError):
                return None
            session = str(meta.get("id") or path.stem)
            source = meta.get("source")
            info = {"id": session, "project": Path(meta.get("cwd") or "").name,
                    "subagent": isinstance(source, dict) and "subagent" in source}
            saved = self.app.store.get_state("cursor:" + str(path))
            if saved is not None and saved <= stat.st_size:
                offset = saved
            elif stat.st_mtime >= self.boot and timestamp(meta.get("timestamp")) >= self.boot:
                offset = 0
            else:
                offset = stat.st_size
                # Seed only recent quota information, never old completion notifications.
                file.seek(max(0, stat.st_size - 1024 * 1024))
                if file.tell():
                    file.readline(MAX_LINE)
                latest = {}
                for line in file:
                    try:
                        row = json.loads(line)
                        p = row.get("payload", {})
                        if row.get("type") == "event_msg" and p.get("type") == "token_count":
                            limits = p.get("rate_limits")
                            if isinstance(limits, dict):
                                key = limits.get("limit_id") or "codex"
                                observed = timestamp(row.get("timestamp"))
                                if observed >= latest.get(key, ({}, 0))[1]:
                                    latest[key] = (limits, observed)
                    except (ValueError, TypeError, AttributeError):
                        continue
                for limits, observed in latest.values():
                    self.usage(limits, observed)
            info["offset"] = offset
            self.app.store.set_state("cursor:" + str(path), offset)
            return info

    def scan(self):
        if not self.root.is_dir():
            self.app.watcher_status.update(error="Codex sessions directory does not exist", last_scan=time.time())
            return
        paths = []
        for path in self.root.rglob("*.jsonl"):
            try:
                stat = path.stat()
                if stat.st_mtime >= self.boot - 86400 or path in self.files:
                    paths.append((path, stat))
            except OSError:
                continue
        # Seed newest usage first, so old sessions cannot cause false low-quota alerts.
        paths.sort(key=lambda item: item[1].st_mtime, reverse=True)
        for path, stat in paths:
            try:
                info = self.files.get(path)
                if info is None:
                    info = self.discover(path, stat)
                    if info is None:
                        continue
                    self.files[path] = info
                if stat.st_size < info["offset"]:
                    info["offset"] = 0
                if stat.st_size == info["offset"]:
                    continue
                with path.open("rb") as file:
                    file.seek(info["offset"])
                    for _ in range(2000):
                        start = file.tell()
                        line = file.readline(MAX_LINE)
                        if not line or not line.endswith(b"\n"):
                            # Leave partial writes untouched until the next scan.
                            if len(line) == MAX_LINE:
                                while line and not line.endswith(b"\n"):
                                    line = file.readline(MAX_LINE)
                                info["offset"] = file.tell() if line.endswith(b"\n") else start
                            break
                        try:
                            row = json.loads(line)
                        except ValueError:
                            info["offset"] = file.tell()
                            continue
                        if isinstance(row, dict):
                            self.process(row, info, start)
                        info["offset"] = file.tell()
                self.app.store.set_state("cursor:" + str(path), info["offset"])
            except OSError:
                continue
        self.app.watcher_status.update(error=None, last_scan=time.time(), files=len(self.files))

    def run(self):
        while not self.app.stop_event.is_set():
            try:
                self.scan()
            except Exception:
                self.app.watcher_status.update(error="Watcher error; inspect service.log")
                LOG.exception("Codex watcher error")
            self.app.stop_event.wait(self.app.cfg["codex"]["poll_seconds"])
