"""Local Notify: python notify.py serve | send | setup | status | recap | hooks | phone | gateway | relay."""
import argparse
import hashlib
import hmac
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
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


# ---------------------------------------------------------------- relay: link this PC's bus to the other PCs (docs/RELAY.md)

DEFAULT_RELAY_URL = "https://146-190-249-220.sslip.io"
PAIR_TIMEOUT = 300
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
PAIR_ERRORS = {"bad_code": "the pairing code is not valid", "code_used": "that pairing code was already used",
               "code_expired": "that pairing code expired", "forbidden": "that planner account is not allowed to pair",
               "bad_machine": "the server refused the machine name"}


def read_raw_config(path):
    """config.json exactly as written (no defaults merged), so saving it back changes only what we touch."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def save_relay(path, **values):
    raw = read_raw_config(path)
    relay = dict(raw["relay"]) if isinstance(raw.get("relay"), dict) else {}
    relay.update(values)
    config.validate_relay(dict({"url": "", "token": "", "machine": ""}, **relay))
    raw["relay"] = relay
    config.save(raw, path)


def relay_opener(url):
    loopback = urllib.parse.urlsplit(url).hostname in ("127.0.0.1", "localhost")
    return urllib.request.build_opener(urllib.request.ProxyHandler({})) if loopback else urllib.request.build_opener()


def redeem_pairing(url, code, machine):
    """POST /api/bus/relay/pair -> {token, machine, createdAt}; the token is returned, never printed."""
    body = json.dumps({"code": code, "machine": machine}).encode("utf-8")
    req = urllib.request.Request(url + "/api/bus/relay/pair", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Planner": "1", "Accept": "application/json",
                                          "User-Agent": "agent-bus-relay/1.0"})
    try:
        with relay_opener(url).open(req, timeout=15) as response:
            answer = json.loads(response.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(4000) or b"{}").get("error")
        except (ValueError, AttributeError, OSError):
            error = None
        raise ValueError(f"the planner refused the pairing (HTTP {exc.code}: {PAIR_ERRORS.get(error, error or 'no detail')})") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ValueError(f"could not reach the planner at {url} ({type(exc).__name__})") from None
    token, paired = answer.get("token") if isinstance(answer, dict) else None, answer.get("machine") if isinstance(answer, dict) else None
    if not isinstance(token, str) or not config.RELAY_TOKEN.fullmatch(token):
        raise ValueError("the planner answered without a usable bus token")
    if not isinstance(paired, str) or not config.MACHINE.fullmatch(paired):
        paired = machine
    return {"token": token, "machine": paired}


class LinkServer(HTTPServer):
    """One-shot loopback listener for the planner's link-desktop redirect (127.0.0.1:<port>/planner-link)."""
    allow_reuse_address = False  # never share a port another program is using
    timeout = 0.5

    def __init__(self, port, nonce, redeem):
        super().__init__(("127.0.0.1", port), LinkHandler)
        self.nonce, self.redeem, self.result = nonce, redeem, None


class LinkHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def page(self, status, title, text):
        body = (f"<!doctype html><html lang=en><meta charset=utf-8><title>{html.escape(title)}</title>"
                f"<body style=\"font-family:system-ui,sans-serif;max-width:36em;margin:4em auto;padding:0 1em\">"
                f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></body></html>").encode("utf-8")
        self.send_response(status)
        for name, value in (("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY")):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server = self.server
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/planner-link":
            return self.page(404, "Not found", "This address only accepts the planner's pairing link.")
        if self.headers.get("Host", "") != f"127.0.0.1:{server.server_port}":
            return self.page(403, "Refused", "Open the link exactly as the planner gives it (127.0.0.1).")
        query = urllib.parse.parse_qs(url.query)
        code, nonce = (query.get("code") or [""])[0], (query.get("nonce") or [""])[0]
        if not NONCE_RE.fullmatch(nonce) or not hmac.compare_digest(nonce.encode(), server.nonce.encode()):
            return self.page(403, "Wrong link", "This link does not belong to the pairing running on this PC.")
        if not 1 <= len(code) <= 200:
            return self.page(400, "No code", "The link has no pairing code. Run python notify.py relay pair again.")
        try:
            server.result = server.redeem(code)
        except ValueError as exc:
            server.result = exc
            return self.page(502, "Pairing failed", f"{str(exc).capitalize()}. Run python notify.py relay pair again.")
        self.page(200, "Paired", f"The Agent Bus on {server.result['machine']} is linked to the planner. You can close this tab.")


def open_link_server(nonce, redeem):
    rng = secrets.SystemRandom()
    for _ in range(50):
        try:
            return LinkServer(rng.randint(1024, 65535), nonce, redeem)
        except OSError:
            continue
    raise ValueError("could not find a free local port for the pairing link")


def format_relay(relay):
    if not relay.get("enabled"):
        return (f"Relay: off. This PC would join as {relay.get('machine')}; pair it with: python notify.py relay pair"
                + (f" (server {relay['server']})" if relay.get("server") else ""))
    state = "connected" if relay.get("connected") else relay.get("status") or "not connected"
    lines = [f"Relay: {state} - this PC is {relay.get('machine')}, server {relay.get('server')}"]
    stamp = lambda value: value.replace("T", " ")[:19] if isinstance(value, str) else "never"
    line = (f"  last push {stamp(relay.get('last_push_at'))}, last pull {stamp(relay.get('last_pull_at'))}, "
            f"state version {relay.get('state_version') or 0}")
    if relay.get("pending_state"):
        line += f", state change waiting to be sent: {relay['pending_state']}"
    lines.append(line)
    others = relay.get("remote_machines") or []
    lines.append("  other PCs: " + (", ".join(f"{m.get('id')} ({'online' if m.get('online') else 'offline'}, "
                                              f"{m.get('agents', 0)} agents)" for m in others)
                                    if others else "none yet (pair the other computer too)"))
    if relay.get("status") == "unauthorized":
        lines.append("  The server refused this PC's bus token: pair again with python notify.py relay pair")
    if relay.get("last_error"):
        lines.append(f"  last error: {relay['last_error']}")
    return "\n".join(lines)


def relay_wait(path, wait, fetch=None):
    """Poll the local service's /health until the relay connects (the Windows service reloads config within ~15 s)."""
    fetch = fetch or (lambda: request("/health", cfg=config.load(path, validate=False), timeout=4))
    deadline = time.monotonic() + max(0.0, wait)
    relay, runtime, reachable = None, None, False
    print("Waiting for the Local Notify service to pick up the new config (the Windows service reloads it within ~15 s)...")
    while True:
        try:
            health = fetch()
            reachable, runtime = True, health.get("runtime")
            relay = (health.get("bus") or {}).get("relay")
            if relay and relay.get("connected"):
                print(format_relay(relay))
                return 0
            if runtime != "windows-service" and not (relay and relay.get("enabled")):
                print("The service is not running as the Windows service, so it does not reload config by itself: "
                      "run .\\Start-Notify.ps1 -Restart, then: python notify.py relay status")
                return 0
        except (urllib.error.URLError, OSError, ValueError):
            pass  # restarting to load the new config
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    if not reachable:
        print("The local service did not answer. Start it (.\\Start-Notify.ps1), then: python notify.py relay status")
    elif relay is None:
        print("The running service has no relay support yet; update this folder and restart it (.\\Start-Notify.ps1 -Restart).")
    else:
        print("Not connected yet (pairing is saved).\n" + format_relay(relay))
    return 1


def relay_pair(machine=None, url=None, no_browser=False, opener=None, path=None, wait=60, fetch=None):
    path = Path(path or config.CONFIG_PATH)
    cfg = config.load(path, validate=False)
    current_url, _, _, _ = config.relay_settings(cfg)
    url = (url or current_url or DEFAULT_RELAY_URL).strip().rstrip("/")
    if not config.RELAY_URL.fullmatch(url):
        raise ValueError("--url must be https://... (or http://127.0.0.1:<port> for local testing)")
    configured = (cfg.get("relay") or {}).get("machine") if isinstance(cfg.get("relay"), dict) else None
    machine = (machine or configured or config.default_machine()).strip()
    if not config.MACHINE.fullmatch(machine):
        raise ValueError("--machine must be 1-40 letters, digits, . _ - starting with a letter or digit")
    nonce = secrets.token_urlsafe(24)
    server = open_link_server(nonce, lambda code: redeem_pairing(url, code, machine))
    link = f"{url}/#/link-desktop?" + urllib.parse.urlencode(
        {"purpose": "bus", "machine": machine, "port": server.server_port, "nonce": nonce})
    print(f"Pairing the Agent Bus on {machine} with {url}.")
    if no_browser:
        print("Open this link in a browser where you are signed in to the planner (within 5 minutes):\n" + link)
    else:
        print("Opening the planner in your browser; sign in if asked. If nothing opens, open this link:\n" + link)
        try:
            (opener or webbrowser.open)(link)
        except Exception:
            print("Could not open a browser; open the link above yourself.")
    deadline = time.monotonic() + PAIR_TIMEOUT
    try:
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if server.result is None:
        raise ValueError("no answer from the planner within 5 minutes; run python notify.py relay pair again")
    if isinstance(server.result, Exception):
        raise server.result
    paired = server.result
    save_relay(path, url=url, token=paired["token"], machine=paired["machine"])
    print(f"Paired as {paired['machine']}. Saved relay.url, relay.token and relay.machine in {path} "
          "(the token stays in that file and is never printed).")
    return relay_wait(path, wait, fetch)


def relay_status(as_json=False, path=None):
    cfg = config.load(Path(path or config.CONFIG_PATH), validate=False)
    health = request("/health", cfg=cfg)
    relay = (health.get("bus") or {}).get("relay")
    if relay is None:
        raise ValueError("the running service has no relay support yet; restart it after updating: .\\Start-Notify.ps1 -Restart")
    print(json.dumps(relay, indent=2, ensure_ascii=True) if as_json else format_relay(relay))
    return 0


def relay_unpair(path=None):
    path = Path(path or config.CONFIG_PATH)
    relay = read_raw_config(path).get("relay")
    if not isinstance(relay, dict) or not relay.get("token"):
        print("This PC is not paired (relay.token is empty).")
        return 0
    save_relay(path, token="")
    print("Removed this PC's bus token from config.json; the Windows service stops relaying within ~15 s "
          "(standalone: .\\Start-Notify.ps1 -Restart). Also remove this computer in planner Settings > Computers "
          "to revoke the token on the server.")
    return 0


def relay_command(args):
    if args.relay_action == "pair":
        return relay_pair(args.machine, args.url, args.no_browser)
    if args.relay_action == "status":
        return relay_status(args.json)
    return relay_unpair()


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
    relay_parser = sub.add_parser("relay", help="Link this PC's Agent Bus with your other PCs through the planner: "
                                                "pair, status, unpair (docs/RELAY.md)")
    relay_sub = relay_parser.add_subparsers(dest="relay_action", required=True)
    pair_parser = relay_sub.add_parser("pair", help="Pair this PC through the planner in your browser")
    pair_parser.add_argument("--machine", help="this PC's name on the relay (default: its hostname)")
    pair_parser.add_argument("--url", help=f"planner server (default: relay.url from config, else {DEFAULT_RELAY_URL})")
    pair_parser.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")
    status_parser = relay_sub.add_parser("status", help="Relay connection, cursors and the other PCs")
    status_parser.add_argument("--json", action="store_true")
    relay_sub.add_parser("unpair", help="Forget this PC's bus token (revoke it in planner Settings > Computers too)")
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
        if args.command == "relay":
            return relay_command(args)
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
