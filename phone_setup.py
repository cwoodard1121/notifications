"""Interactive phone setup: Telegram texts, the Cloudflare gateway Worker, Twilio calls, gateway clients.

Secrets are typed into getpass prompts and piped to `wrangler secret put` on stdin. Only the Telegram bot token/chat id
and the bus's own gateway token are written to config.json; nothing Twilio-related touches the disk.
"""
import base64
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import phone
import providers

GATEWAY_DIR = config.ROOT / "gateway"
TELEGRAM_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
TWILIO_SID = re.compile(r"AC[0-9a-fA-F]{32}")
TWILIO_TOKEN = re.compile(r"[0-9a-fA-F]{32}")
CLIENT = re.compile(r"[a-z][a-z0-9_]{0,31}")
TWILIO = "https://api.twilio.com/2010-04-01/Accounts/"
RESTART = "Restart the service so it picks this up: .\\Start-Notify.ps1 -Restart"
SUBDOMAIN = ("Open https://dash.cloudflare.com → Workers & Pages once and choose a workers.dev subdomain, "
             "then run this again.")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TRIAL = ("WARNING: this is a Twilio TRIAL account. Trial calls play a Twilio notice and wait for a key press before your "
         "message, and can only call verified numbers. Upgrade it (Console > Billing > Upgrade, $20 minimum) to fix that.")


class SetupError(Exception):
    pass


def http_json(method, url, body=None, headers=None, form=None, timeout=20):
    """(status, JSON object). Errors name only the host: Telegram puts the bot token in the URL path."""
    headers = dict(headers or {}, **{"User-Agent": "LocalNotify/1.0", "Accept": "application/json"})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), providers.NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, phone.parse(response.read(1 << 20))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, phone.parse(exc.read(1 << 20))
        except OSError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SetupError(f"Could not reach {urllib.parse.urlsplit(url).hostname} (network problem or timeout)") from None


class IO:
    """Everything that touches the user, the network or other programs, so tests can swap it out."""

    def __init__(self, ask=input, ask_secret=getpass.getpass, run=subprocess.run, http=http_json, print=print,
                 which=shutil.which, sleep=time.sleep, clock=time.monotonic):
        self.ask, self.ask_secret, self.run, self.http, self.print = ask, ask_secret, run, http, print
        self.which, self.sleep, self.clock = which, sleep, clock


def yes(io, question):
    return io.ask(question).strip().lower() in ("y", "yes")


def tail(text, lines=15):
    return "\n".join(text.strip().splitlines()[-lines:])


def scrub(text, *values):
    text = ANSI.sub("", text)
    for value in values:
        if value:
            text = text.replace(value, "***")
    return text


def wrangler(io, args, secret=None, timeout=300):
    npx = io.which("npx.cmd") or io.which("npx")
    if not npx:
        raise SetupError("Node.js is needed for wrangler, but npx was not found. Install Node.js LTS from https://nodejs.org, "
                         "open a new terminal, then run this again.")
    if not GATEWAY_DIR.is_dir():
        raise SetupError(f"{GATEWAY_DIR} is missing; the gateway Worker code must be there.")
    # stdin is never a terminal, so wrangler runs non-interactively: it reads a secret from the pipe, and any prompt
    # (e.g. "register a workers.dev subdomain?") gets end-of-file instead of waiting forever.
    stdin = {"input": secret} if secret else {"stdin": subprocess.DEVNULL}
    try:
        result = io.run([npx, "--yes", "wrangler@4", *args], cwd=str(GATEWAY_DIR), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=timeout, **stdin)
    except subprocess.TimeoutExpired:
        raise SetupError(f"wrangler {args[0]} timed out") from None
    except OSError as exc:
        raise SetupError(f"Could not run npx: {exc.strerror or exc}") from None
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, scrub(output, secret, os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_API_KEY"))


def put_secret(io, name, value):
    code, output = wrangler(io, ["secret", "put", name], secret=value)
    if code != 0:
        raise SetupError(f"Uploading {name} to the Worker failed:\n{tail(output)}")
    io.print(f"  uploaded {name} to the Worker")


def load(path):
    return config.load(path or config.CONFIG_PATH, validate=False)


def save(cfg, path):
    config.save(cfg, path or config.CONFIG_PATH)


def gateway_health(io, url):
    try:
        status, data = io.http("GET", url + "/v1/health")
    except SetupError as exc:
        io.print(f"The gateway is not answering yet ({exc}); check again in a minute: python notify.py phone status")
        return None
    if status != 200 or not data.get("ok"):
        io.print(f"The gateway answered HTTP {status}; check again in a minute: python notify.py phone status")
        return None
    io.print(f"Gateway health: calls {'ready' if data.get('twilio') else 'not set up yet (python notify.py setup twilio)'}; "
             f"texts {'ready' if data.get('telegram') else 'not set up yet (python notify.py setup telegram)'}"
             + ("; DRY-RUN mode (nothing really rings)" if data.get("dry_run") else ""))
    return data


# ---- Telegram
def setup_telegram(io, primary=False, path=None):
    cfg = load(path)
    io.print("Telegram texts. Create a bot first: in Telegram open @BotFather, send /newbot, pick a name, copy the token.")
    token = io.ask_secret("Bot token (input hidden): ").strip()
    if not TELEGRAM_TOKEN.fullmatch(token):
        raise SetupError("That does not look like a bot token (digits, a colon, then 30+ letters). Copy it again from @BotFather.")
    api = f"https://api.telegram.org/bot{token}/"
    status, me = io.http("GET", api + "getMe")
    if status != 200 or not me.get("ok"):
        raise SetupError(f"Telegram did not accept that token (HTTP {status}). Copy it again from @BotFather.")
    bot = (me.get("result") or {}).get("username") or "your bot"
    io.print(f"Open Telegram, find @{bot}, press Start. Waiting up to 3 minutes...")
    chat_id = wait_for_chat(io, api)
    cfg["telegram"].update(bot_token=token, chat_id=chat_id)
    if primary:
        cfg["provider"] = "telegram"
    save(cfg, path)
    io.print("Saved telegram.bot_token and telegram.chat_id in config.json" + (" and made Telegram the main provider." if primary else
             f"; {cfg['provider']} stays the main provider and priority 4+ also texts you."))
    status, sent = io.http("POST", api + "sendMessage", {"chat_id": chat_id, "text": "✅ Agent Bus texts are set up."})
    if status != 200 or not sent.get("ok"):
        io.print(f"Warning: the confirmation text was not accepted (HTTP {status}).")
    if (cfg.get("gateway") or {}).get("url"):
        put_secret(io, "TELEGRAM_BOT_TOKEN", token)
        put_secret(io, "TELEGRAM_CHAT_ID", chat_id)
    io.print(RESTART)
    return 0


def wait_for_chat(io, api):
    deadline = io.clock() + 180
    offset, cleared = None, False
    while io.clock() < deadline:
        query = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            query["offset"] = offset
        status, data = io.http("GET", api + "getUpdates?" + urllib.parse.urlencode(query), timeout=40)
        if status == 409 and not cleared:  # a webhook is set: long-polling is refused until it is removed
            cleared = True
            io.http("POST", api + "deleteWebhook", {})
            continue
        if status != 200 or not data.get("ok"):
            raise SetupError(f"Telegram getUpdates failed (HTTP {status}).")
        for update in data.get("result") or []:
            if type(update.get("update_id")) is int:
                offset = update["update_id"] + 1
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private" and chat.get("id"):
                io.print(f"Got it: chat with {chat.get('first_name') or chat.get('username') or 'you'} (id {chat['id']}).")
                return str(chat["id"])
    raise SetupError("No message reached the bot within 3 minutes. Press Start in the bot's chat, then run this again.")


# ---- Cloudflare gateway Worker
def setup_gateway(io, rotate=False, path=None):
    cfg = load(path)
    io.print("Checking your Cloudflare login (wrangler whoami)...")
    code, output = wrangler(io, ["whoami"])
    if code != 0 or "not authenticated" in output.lower() or "not logged in" in output.lower():
        io.print(f"Run: cd {GATEWAY_DIR} ; npx wrangler login  (a browser opens; click Allow), then run this again")
        return 1
    io.print("Deploying the gateway Worker (wrangler deploy)...")
    code, output = wrangler(io, ["deploy"])
    match = re.search(r"https://agent-bus-phone\.[A-Za-z0-9-]+\.workers\.dev", output)
    if not match:
        # Usually a brand-new Cloudflare account: it has no workers.dev subdomain, which wrangler only offers interactively.
        io.print(f"wrangler deploy (exit code {code}) printed no https://agent-bus-phone.<subdomain>.workers.dev URL. "
                 f"wrangler said:\n{tail(output, 25)}\n")
        io.print(SUBDOMAIN)
        return 1
    if code != 0:
        raise SetupError("wrangler deploy failed:\n" + tail(output))
    url = match.group(0)
    token = (cfg.get("gateway") or {}).get("token") or ""
    if rotate or len(token) < 32:
        token = "gw_bus_" + secrets.token_urlsafe(32)
    put_secret(io, "TOKEN_BUS", token)
    cfg["gateway"] = dict(cfg.get("gateway") or {}, url=url, token=token)
    save(cfg, path)  # right away, so a later failure cannot leave the Worker and config.json with different bus tokens
    telegram = cfg.get("telegram") or {}
    if telegram.get("bot_token") and telegram.get("chat_id"):
        put_secret(io, "TELEGRAM_BOT_TOKEN", telegram["bot_token"])
        put_secret(io, "TELEGRAM_CHAT_ID", str(telegram["chat_id"]))
    io.print(f"Gateway: {url} (URL and the bus token saved in config.json)")
    gateway_health(io, url)
    io.print(RESTART)
    return 0


def add_client(io, name, path=None):
    if not CLIENT.fullmatch(name) or name == "bus":
        raise SetupError("A client name is lowercase letters, digits and _ (starting with a letter, at most 32); "
                         "'bus' is this PC's own client.")
    url = (load(path).get("gateway") or {}).get("url")
    if not url:
        raise SetupError("Set up the gateway first: python notify.py setup gateway")
    token = f"gw_{name}_" + secrets.token_urlsafe(32)
    put_secret(io, "TOKEN_" + name.upper(), token)
    io.print(f"Set these in that server's environment:\n  PHONE_GATEWAY_URL={url}\n  PHONE_GATEWAY_TOKEN={token}\n"
             "It is not stored on this PC; run this again to rotate.")
    return 0


# ---- Twilio
def twilio_message(data):
    return str(data.get("message") or data.get("detail") or "no details")[:300]


def e164(raw):
    digits = re.sub(r"[^\d+]", "", raw.strip())
    if re.fullmatch(r"\d{10}", digits):
        return "+1" + digits
    if re.fullmatch(r"1\d{10}", digits):
        return "+" + digits
    return digits if re.fullmatch(r"\+[1-9]\d{6,14}", digits) else None


def pick_number(io, base, headers):
    status, data = io.http("GET", base + "/IncomingPhoneNumbers.json?PageSize=50", headers=headers)
    if status != 200:
        raise SetupError(f"Could not list your Twilio numbers (HTTP {status}): {twilio_message(data)}")
    owned = [n for n in data.get("incoming_phone_numbers") or [] if str(n.get("phone_number", "")).startswith("+1")]
    if len(owned) == 1:
        io.print(f"Using your existing Twilio number {owned[0]['phone_number']}.")
        return owned[0]
    if owned:
        for i, number in enumerate(owned, 1):
            io.print(f"  {i}. {number['phone_number']}  {number.get('friendly_name') or ''}")
        choice = io.ask(f"Which number should call you? [1-{len(owned)}]: ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(owned):
            raise SetupError("No number chosen; nothing was changed.")
        return owned[int(choice) - 1]
    candidate = None
    for area in ("519", "226", "548"):
        query = urllib.parse.urlencode({"AreaCode": area, "VoiceEnabled": "true", "PageSize": 5})
        status, data = io.http("GET", f"{base}/AvailablePhoneNumbers/CA/Local.json?{query}", headers=headers)
        found = data.get("available_phone_numbers") if status == 200 else None
        if found:
            candidate = found[0]["phone_number"]
            break
    if not candidate:
        raise SetupError("Twilio has no 519/226/548 numbers right now. Buy a Canadian number in the Twilio Console "
                         "(Phone Numbers > Manage > Buy a number), then run this again; it will be reused.")
    if not yes(io, f"Buy {candidate} for about US$1.15/month? [y/N]: "):
        io.print("Nothing bought.")
        return None
    status, bought = io.http("POST", base + "/IncomingPhoneNumbers.json", headers=headers,
                             form={"PhoneNumber": candidate, "FriendlyName": "Agent Bus"})
    if status not in (200, 201) or not bought.get("sid"):
        io.print(f"Twilio would not sell {candidate}: {twilio_message(bought)}")
        io.print("Buy a Canadian number in the Twilio Console (Phone Numbers > Manage > Buy a number), then run this "
                 "again; it will be reused.")
        return None
    io.print(f"Bought {bought['phone_number']}.")
    return bought


def setup_twilio(io, path=None):
    url = ((load(path).get("gateway") or {}).get("url") or "").rstrip("/")
    if not url:
        raise SetupError("Set up the gateway first: python notify.py setup gateway")
    io.print("Twilio calls. Both values are on the Twilio Console home page (Account Info).")
    sid = io.ask("Account SID (starts with AC): ").strip()
    if not TWILIO_SID.fullmatch(sid):
        raise SetupError("An Account SID is AC followed by 32 hex characters.")
    auth = io.ask_secret("Auth Token (input hidden): ").strip()
    if not TWILIO_TOKEN.fullmatch(auth):
        raise SetupError("The Auth Token is 32 hex characters (Console > Account Info > Auth Token > show).")
    headers = {"Authorization": "Basic " + base64.b64encode(f"{sid}:{auth}".encode()).decode()}
    base = TWILIO + sid
    status, account = io.http("GET", base + ".json", headers=headers)
    if status == 401:
        raise SetupError("Twilio did not accept that Account SID and Auth Token.")
    if status != 200:
        raise SetupError(f"Twilio account check failed (HTTP {status}): {twilio_message(account)}")
    if account.get("type") == "Trial":
        io.print(TRIAL)
    number = pick_number(io, base, headers)
    if number is None:
        return 1
    status, data = io.http("POST", f"{base}/IncomingPhoneNumbers/{number['sid']}.json", headers=headers,
                           form={"VoiceUrl": url + "/twilio/incoming", "VoiceMethod": "POST"})
    if status != 200:
        raise SetupError(f"Could not point {number['phone_number']} at the gateway (HTTP {status}): {twilio_message(data)}")
    io.print(f"Calling {number['phone_number']} back now reaches the gateway (it replays your newest open alert).")
    for _ in range(3):
        cell = e164(io.ask("Your cell number, the only number the gateway will ever call (e.g. 519 555 0123): "))
        if cell:
            break
        io.print("That is not a phone number; type 10 digits, or +country code and number.")
    else:
        raise SetupError("No valid cell number; the Twilio keys were not uploaded.")
    for name, value in (("TWILIO_ACCOUNT_SID", sid), ("TWILIO_AUTH_TOKEN", auth), ("TWILIO_FROM", number["phone_number"]),
                        ("OWNER_NUMBER", cell)):
        put_secret(io, name, value)
    gateway_health(io, url)
    io.print(f"Save {number['phone_number']} in your contacts as 'Agent Bus' (so the call shows a name and gets past "
             "silence-unknown-callers).")
    return 0


# ---- everything, in order
def test_call(io, path=None):
    gateway = load(path).get("gateway") or {}
    url, headers = gateway["url"].rstrip("/"), {"Authorization": "Bearer " + gateway["token"]}
    body = {"ref": "bus-test-" + secrets.token_hex(6), "title": "Test call",
            "message": "This is a test call from your Agent Bus. Press 1 to acknowledge.", "channels": ["call"],
            "retry": False, "fallback_text": False}
    status, data = io.http("POST", url + "/v1/alerts", body, headers=headers)
    alert = data.get("alert") or {}
    if status not in (200, 201) or not alert.get("id"):
        raise SetupError(f"The gateway did not place the test call (HTTP {status}): {data.get('detail') or data.get('error')}")
    io.print("Calling you now: answer and press 1.")
    last, deadline = None, io.clock() + 150
    while io.clock() < deadline:
        status, data = io.http("GET", f"{url}/v1/alerts/{urllib.parse.quote(alert['id'], safe='')}", headers=headers)
        current = (data.get("alert") or {}).get("status") if status == 200 else f"HTTP {status}"
        if current != last:
            io.print(f"  call: {current}")
            last = current
        if current in phone.FINAL:
            break
        io.sleep(3)
    io.print("It works." if last == "acked" else "The test call did not end acknowledged; see docs/PHONE-ESCALATION.md (Troubleshooting).")
    return 0


def setup_phone(io, path=None):
    telegram = load(path).get("telegram") or {}
    if not (telegram.get("bot_token") and telegram.get("chat_id")) or yes(io, "Telegram texts are already set up. Set them up again? [y/N]: "):
        setup_telegram(io, path=path)
    if setup_gateway(io, path=path) or setup_twilio(io, path=path):
        return 1
    if yes(io, "Place a test call now? [y/N]: "):
        test_call(io, path)
    return 0


def cli(function, *args, **kwargs):
    for stream in (sys.stdout, sys.stderr):  # wrangler's output has symbols a cp1252 pipe cannot encode
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        return function(IO(), *args, **kwargs)
    except SetupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopped; nothing more was changed.", file=sys.stderr)
        return 130
    except (EOFError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
    return 1
