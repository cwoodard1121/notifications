"""Small JSON config; secrets stay in the ignored config.json file."""
import copy
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("NOTIFY_CONFIG", ROOT / "config.json"))
DATA_DIR = Path(os.environ.get("NOTIFY_DATA_DIR", ROOT / "data"))


def merge(base, values):
    result = copy.deepcopy(base)
    for key, value in values.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def load(path=CONFIG_PATH, validate=True):
    defaults = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    cfg = merge(defaults, json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else defaults
    if not validate:
        return cfg
    if type(cfg["port"]) is not int or not 1 <= cfg["port"] <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if cfg["provider"] not in ("local", "ntfy", "telegram", "whatsapp"):
        raise ValueError("provider must be local, ntfy, telegram, or whatsapp")
    if not isinstance(cfg["api_token"], str):
        raise ValueError("api_token must be a string")
    if cfg["provider"] == "ntfy":
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", cfg["ntfy"]["topic"]):
            raise ValueError("Set an ntfy topic (or run: python notify.py setup ntfy)")
        if not cfg["ntfy"]["server"].startswith("https://"):
            raise ValueError("ntfy.server must use HTTPS")
    required = {"telegram": ("bot_token", "chat_id"), "whatsapp": ("account_sid", "auth_token", "from", "to")}
    for field in required.get(cfg["provider"], ()):
        if not cfg[cfg["provider"]][field]:
            raise ValueError(f"Set {cfg['provider']}.{field} in config.json")
    if cfg["provider"] == "whatsapp":
        wa = cfg["whatsapp"]
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", wa["account_sid"]):
            raise ValueError("Invalid WhatsApp Twilio account_sid")
        for field in ("from", "to"):
            if not re.fullmatch(r"whatsapp:\+[1-9][0-9]{6,14}", wa[field]):
                raise ValueError(f"whatsapp.{field} must be whatsapp:+COUNTRYNUMBER")
    if type(cfg["codex"]["poll_seconds"]) not in (int, float) or not 1 <= cfg["codex"]["poll_seconds"] <= 300:
        raise ValueError("codex.poll_seconds must be between 1 and 300")
    thresholds = cfg["codex"]["remaining_thresholds"]
    if not isinstance(thresholds, list) or any(type(n) is not int or not 0 <= n <= 100 for n in thresholds):
        raise ValueError("codex.remaining_thresholds must be a list of percentages")
    daily = cfg["recap"]["daily_at"]
    if daily and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", daily):
        raise ValueError("recap.daily_at must be HH:MM (local computer time), or empty")
    if type(cfg["recap"]["hours"]) not in (int, float) or not 0 < cfg["recap"]["hours"] <= 720:
        raise ValueError("recap.hours must be between 0 and 720")
    if type(cfg["retention_days"]) is not int or not 1 <= cfg["retention_days"] <= 365:
        raise ValueError("retention_days must be between 1 and 365")
    for section in ("claude", "codex"):
        if not isinstance(cfg[section].get("auto_notify", False), bool):
            raise ValueError(f"{section}.auto_notify must be true or false")
    validate_phone(cfg.get("phone", {}), cfg.get("gateway", {}))
    validate_bus(cfg.get("bus", {}))
    return cfg


def number(value, low, high):
    return type(value) in (int, float) and low <= value <= high


def validate_phone(phone, gateway):
    if not isinstance(phone, dict) or not isinstance(gateway, dict):
        raise ValueError("phone and gateway must be objects")
    if phone.get("text_channel", "telegram") not in ("telegram", "off"):
        raise ValueError("phone.text_channel must be telegram or off")
    for key in ("text_min_priority", "call_min_priority"):
        if type(phone.get(key, 4)) is not int or not 1 <= phone.get(key, 4) <= 5:
            raise ValueError(f"phone.{key} must be an integer from 1 to 5")
    for key in ("max_calls_per_hour", "max_calls_per_day"):
        if type(phone.get(key, 4)) is not int or not 0 <= phone.get(key, 4) <= 100:
            raise ValueError(f"phone.{key} must be an integer from 0 to 100")
    if not number(phone.get("poll_seconds", 5), 2, 300):
        raise ValueError("phone.poll_seconds must be between 2 and 300")
    url = gateway.get("url", "")
    if not isinstance(url, str) or (url and not re.fullmatch(
            r"https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?|http://(?:127\.0\.0\.1|localhost):\d{1,5}/?", url)):
        raise ValueError("gateway.url must be empty, https://..., or http://127.0.0.1:<port> for local testing "
                         "(run: python notify.py setup gateway)")
    if not isinstance(gateway.get("token", ""), str):
        raise ValueError("gateway.token must be a string")


def validate_bus(bus):
    # Optional keys may be missing; defaults come from config.example.json.
    if not isinstance(bus, dict):
        raise ValueError("bus must be an object")
    for key in ("wake_on_stop", "idle_wake"):
        if not isinstance(bus.get(key, True), bool):
            raise ValueError(f"bus.{key} must be true or false")
    if not number(bus.get("recent_broadcast_hours", 2), 0, 168):
        raise ValueError("bus.recent_broadcast_hours must be between 0 and 168")
    modes = bus.get("modes", {})
    if not isinstance(modes, dict):
        raise ValueError("bus.modes must be an object")
    for name, mode in modes.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name) or not isinstance(mode, dict):
            raise ValueError(f"bus.modes.{name}: use a lowercase name and an object value")
        if type(mode.get("phone_min_priority", 1)) is not int or not 1 <= mode.get("phone_min_priority", 1) <= 5:
            raise ValueError(f"bus.modes.{name}.phone_min_priority must be an integer from 1 to 5")
        if not number(mode.get("ttl_minutes", 0), 0, 1440):
            raise ValueError(f"bus.modes.{name}.ttl_minutes must be between 0 and 1440")
        if not all(isinstance(mode.get(key, ""), str) for key in ("label", "directive")):
            raise ValueError(f"bus.modes.{name}.label and directive must be strings")
    mailbox = bus.get("mailbox", {})
    if not isinstance(mailbox, dict) or not isinstance(mailbox.get("enabled", False), bool):
        raise ValueError("bus.mailbox.enabled must be true or false")
    dirs = mailbox.get("dirs", [])
    if not isinstance(dirs, list) or not all(isinstance(d, str) and d for d in dirs):
        raise ValueError("bus.mailbox.dirs must be a list of folder paths")
    if not number(mailbox.get("poll_seconds", 1), 0.2, 60):
        raise ValueError("bus.mailbox.poll_seconds must be between 0.2 and 60")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,79}", str(mailbox.get("agent_id", "study-claude"))):
        raise ValueError("bus.mailbox.agent_id must be a simple id such as study-claude")


def save(cfg, path=CONFIG_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
