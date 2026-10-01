"""Local Notify: python notify.py serve | send | setup | status | recap | hooks | phone | gateway."""
import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config
import phone_setup
from service import App, Server

INTERACTIVE = {"telegram": phone_setup.setup_telegram, "gateway": phone_setup.setup_gateway,
               "twilio": phone_setup.setup_twilio, "phone": phone_setup.setup_phone}


def request(path, payload=None, cfg=None, timeout=4):
    cfg = cfg or config.load()
    headers = {"Content-Type": "application/json"}
    if cfg["api_token"]:
        headers["Authorization"] = "Bearer " + cfg["api_token"]
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}{path}", data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def hook_payload(source, data, cfg):
    # Automatic "finished"/"needs attention" pings are off unless explicitly re-enabled: phone
    # notifications only come from things an agent intentionally sends.
    if not isinstance(data, dict) or not cfg["codex" if source == "codex" else "claude"].get("auto_notify"):
        return None
    if source == "codex":
        if data.get("type") != "agent-turn-complete":
            return None
        event, title = "done", "Codex finished"
        message = data.get("last-assistant-message") if cfg["codex"]["include_summary"] else None
        message = message or "Your Codex turn has finished."
        key = f"codex:{data.get('thread-id', '')}:{data.get('turn-id', '')}"
        if not data.get("turn-id"):
            key += hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]
        priority = 3
    else:
        kind = data.get("hook_event_name")
        if kind == "Stop":
            # A Stop hook may be re-fired while another hook continues the turn.
            if data.get("stop_hook_active") or data.get("background_tasks") or data.get("session_crons"):
                return None
            event, title, priority = "done", "Claude finished", 3
            message = data.get("last_assistant_message") if cfg["claude"]["include_summary"] else None
            message = message or "Your Claude Code turn has finished."
        elif kind == "Notification":
            event, title, priority = "attention", "Claude needs your attention", 4
            message = data.get("message") or "Claude Code is waiting for you."
        else:
            return None
        # Stop lacks a turn ID. Suppress immediate repeats, allow identical future replies.
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]
        key = f"claude:{data.get('session_id', '')}:{kind}:{digest}:{int(time.time() // 30)}"
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        title += " — " + Path(cwd).name
    return {"title": title[:150], "message": str(message)[:2500], "source": source,
            "event": event, "priority": priority, "dedupe_key": key[:300]}


def run_hook(source, raw):
    # Hooks must never block an agent from completing, even if the service is off.
    try:
        cfg = config.load()
        payload = hook_payload(source, json.loads(raw), cfg)
        if payload:
            request("/notify", payload, cfg, timeout=2)
    except Exception:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("notify-hook")
        handler = RotatingFileHandler(config.DATA_DIR / "hooks.log", maxBytes=200000, backupCount=1)
        logger.addHandler(handler)
        logger.warning("%s: notification could not be queued. Is the local service running?", source)
        handler.close()
        logger.removeHandler(handler)


def configure_claude(action, home=None):
    if action == "install":
        return {"action": "install", "installed": False,
                "note": "Automatic Claude Code phone pings (Stop/Notification hooks) were removed. Agents send intentional "
                        "notifications with `python notify.py send` or the notify-user skill, and join the Agent Bus through "
                        "the agent-bus plugin (Install-AgentBus.ps1). Run `python notify.py hooks remove` to delete old hooks."}
    home = home or Path.home()
    path = home / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be a JSON object")
    original = json.dumps(settings, sort_keys=True)
    script = (config.ROOT / "notify.py").as_posix().lower()

    def ours(hook):
        # Match the old installer's command regardless of which Python executable wrote it.
        command = str(hook.get("command", "")).replace("\\", "/").lower()
        return script in command and command.rstrip().endswith("hook claude")

    hooks = settings.get("hooks")
    for event in ("Stop", "Notification") if isinstance(hooks, dict) else ():
        retained = []
        for entry in hooks.get(event, []):
            other_hooks = [h for h in entry.get("hooks", []) if not ours(h)]
            if other_hooks:
                retained.append(dict(entry, hooks=other_hooks))
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    if json.dumps(settings, sort_keys=True) != original:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup_dir = config.DATA_DIR / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_dir / f"claude-settings-{time.time_ns()}.json")
        temporary = path.with_suffix(".notify.tmp")
        temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    return {"action": action, "settings": str(path), "events": ["Stop", "Notification"], "restart_claude": True}


def setup(provider):
    cfg = config.load(validate=False)
    cfg["provider"] = provider
    if provider == "ntfy":
        cfg["ntfy"]["topic"] = cfg["ntfy"]["topic"] or "agents-" + secrets.token_hex(16)
    config.save(cfg)
    result = {"config": str(config.CONFIG_PATH), "provider": provider, "restart_service": True}
    if provider == "ntfy":
        result["subscribe_on_phone"] = cfg["ntfy"]["server"].rstrip("/") + "/" + cfg["ntfy"]["topic"]
        result["note"] = "Add this topic in the ntfy phone app. Treat the topic URL as a secret."
    elif provider in ("telegram", "whatsapp"):
        result["next"] = f"Fill the {provider} credentials in config.json before starting the service."
    return result


def phone_command(args):
    if args.action == "test":
        payload = {"title": "Agent Bus test", "source": "notify-cli", "event": "test"}
        if args.call:
            payload.update(call=True, message="This is a test call from your Agent Bus. Press 1 to acknowledge, 2 to snooze.")
        else:
            payload.update(priority=4, message="Test text from your Agent Bus: priority 4 reaches ntfy and an SMS text.")
        return request("/notify", payload)
    if args.action == "calls":
        return request("/phone/calls?limit=20")
    if args.action == "ack":
        if not args.id:
            raise ValueError("Give the call id: python notify.py phone ack <id> (see: python notify.py phone calls)")
        return request(f"/phone/calls/{urllib.parse.quote(args.id.strip(), safe='')}/ack", {}, timeout=15)
    health = request("/health")
    if "phone" not in health:
        raise ValueError("The running service predates phone escalation; restart it: .\\Start-Notify.ps1 -Restart")
    return health["phone"]


def serve():
    cfg = config.load()
    if not config.CONFIG_PATH.exists():
        config.save(cfg)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(), RotatingFileHandler(config.DATA_DIR / "service.log", maxBytes=1000000, backupCount=2)])
    app = App(cfg, config.DATA_DIR)
    # Bind before starting watcher threads, so a second instance cannot send duplicates.
    server = Server(("127.0.0.1", cfg["port"]), app)
    app.start()
    logging.info("Local Notify listening on http://127.0.0.1:%s (provider: %s)", cfg["port"], cfg["provider"])
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


def main():
    parser = argparse.ArgumentParser(description="Simple localhost notifications for agents and scripts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run the local API and Codex watcher")
    setup_parser = sub.add_parser("setup", help="Choose phone delivery, or set up SMS texts and calls (phone = gateway + twilio; "
                                                "telegram is legacy)")
    setup_parser.add_argument("provider", choices=["local", "ntfy", "telegram", "whatsapp", "gateway", "twilio", "phone"])
    setup_parser.add_argument("--primary", action="store_true", help="telegram (legacy): also make Telegram the main provider")
    setup_parser.add_argument("--rotate", action="store_true", help="gateway: replace the bus token")
    send_parser = sub.add_parser("send", help="Send a notification")
    send_parser.add_argument("message")
    send_parser.add_argument("--title", default="Notification")
    send_parser.add_argument("--source", default="agent")
    send_parser.add_argument("--event", default="message")
    send_parser.add_argument("--priority", type=int, choices=range(1, 6), default=3)
    send_parser.add_argument("--dedupe-key")
    send_parser.add_argument("--call", action=argparse.BooleanOptionalAction, default=None,
                             help="--call rings the phone (implies priority 5, emergencies only); --no-call never rings")
    gateway_parser = sub.add_parser("gateway", help="Phone gateway clients")
    gateway_parser.add_argument("action", choices=["add-client"])
    gateway_parser.add_argument("name")
    phone_parser = sub.add_parser("phone", help="Phone escalation (SMS texts and calls): test [--call], calls, ack <id>, status")
    phone_parser.add_argument("action", choices=["test", "calls", "ack", "status"])
    phone_parser.add_argument("id", nargs="?")
    phone_parser.add_argument("--call", action="store_true", help="test: place a test call (with its SMS text) instead of a text")
    sub.add_parser("status")
    sub.add_parser("events")
    recap_parser = sub.add_parser("recap", help="Preview or send a recap")
    recap_parser.add_argument("--hours", type=float, default=24)
    recap_parser.add_argument("--send", action="store_true")
    hook_parser = sub.add_parser("hook", help="Agent hook bridge (always exits successfully)")
    hook_parser.add_argument("source", choices=["codex", "claude"])
    hook_parser.add_argument("payload", nargs="?")
    hooks_parser = sub.add_parser("hooks", help="Remove the old Claude Code Stop/Notification ping hooks (install is a no-op)")
    hooks_parser.add_argument("action", choices=["install", "remove"])
    args = parser.parse_args()
    if args.command == "hook":
        try:
            raw = args.payload if args.payload is not None else sys.stdin.buffer.read(1024 * 1024).decode("utf-8-sig")
            run_hook(args.source, raw)
        except Exception:
            pass
        return 0
    if args.command == "setup" and args.provider in INTERACTIVE:
        options = {"primary": args.primary} if args.provider == "telegram" else {"rotate": args.rotate} if args.provider == "gateway" else {}
        return phone_setup.cli(INTERACTIVE[args.provider], **options)
    if args.command == "gateway":
        return phone_setup.cli(phone_setup.add_client, args.name)
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "setup":
            result = setup(args.provider)
        elif args.command == "phone":
            result = phone_command(args)
        elif args.command == "send":
            result = request("/notify", {k: v for k, v in vars(args).items() if k != "command" and v is not None})
        elif args.command == "status":
            result = request("/health")
        elif args.command == "events":
            result = request("/events")
        elif args.command == "recap":
            result = request("/recap", {"hours": args.hours}) if args.send else request(f"/recap?hours={args.hours}")
        else:
            result = configure_claude(args.action)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except urllib.error.HTTPError as exc:
        print(f"Local API returned HTTP {exc.code}: {exc.read(2000).decode('utf-8', errors='replace')}", file=sys.stderr)
    except urllib.error.URLError:
        print("Local service is not reachable. Run: python notify.py serve", file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
