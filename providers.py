"""Phone delivery adapters. API acceptance is not a device delivery receipt."""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class DeliveryError(Exception):
    def __init__(self, message, retry=False, delay=0):
        super().__init__(message)
        self.retry = retry
        self.delay = delay


class GatewayDown(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse(raw):
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def gateway_http(method, url, token, body=None):
    """Phone gateway request: (status, JSON object, Retry-After seconds). Raises GatewayDown without the URL or token."""
    headers = {"Authorization": "Bearer " + token, "User-Agent": "LocalNotify/1.0", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, parse(response.read(65536)), 0
    except urllib.error.HTTPError as exc:
        retry_after = (exc.headers.get("Retry-After") if exc.headers else None) or ""
        try:
            body = parse(exc.read(65536))
        except OSError:
            body = {}
        return exc.code, body, int(retry_after) if retry_after.isdigit() else 0
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GatewayDown("gateway connection failed or timed out") from None


def send_sms(cfg, event, gateway=None):
    """Priority-4 text: one text-only alert through the phone gateway (Twilio SMS). Returns the gateway alert id."""
    settings = cfg.get("gateway") or {}
    url, token = (settings.get("url") or "").rstrip("/"), settings.get("token") or ""
    if not url or not token:
        raise DeliveryError("phone gateway not set up (python notify.py setup phone)")
    body = {"ref": "bus-sms-" + event["id"], "title": event["title"][:150], "message": event["message"][:1500],
            "channels": ["text"]}
    try:
        status, data, retry_after = (gateway or gateway_http)("POST", url + "/v1/alerts", token, body)
    except GatewayDown as exc:
        raise DeliveryError(str(exc), retry=True) from None
    alert = data.get("alert") if isinstance(data.get("alert"), dict) else {}

    def detail(value):
        return str(value or "no detail")[:300].replace(token, "***")
    if status in (200, 201) and alert.get("status") == "sent" and alert.get("id"):
        return str(alert["id"])[:100]  # 200 = the same ref again (a retry after a lost response)
    if status in (200, 201) and alert.get("status") == "text_failed":
        raise DeliveryError("SMS not sent: " + detail((alert.get("text") or {}).get("detail")))
    if status == 429:
        wait = data.get("retry_after") if type(data.get("retry_after")) is int else retry_after or 60
        wait = max(1, min(wait, 86400))
        raise DeliveryError(f"gateway text limit reached; trying again in {wait} s", retry=True, delay=wait)
    if status == 503:
        raise DeliveryError("gateway: twilio not configured (python notify.py setup twilio)")
    if status in (401, 403):
        raise DeliveryError(f"gateway refused the bus token (HTTP {status}); run: python notify.py setup gateway")
    if status == 400:
        raise DeliveryError("gateway rejected the text: " + detail(data.get("detail") or data.get("error")))
    if status >= 500 or status in (200, 201):
        raise DeliveryError(f"gateway returned HTTP {status}", retry=True)
    raise DeliveryError(f"gateway returned HTTP {status} (check gateway.url)")


def http_post(url, payload, headers, opener=None):
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After", "0")
        delay = min(int(retry_after), 3600) if retry_after.isdigit() else 0
        # Do not store URLs, auth headers, or upstream bodies containing credentials.
        raise DeliveryError(f"Provider returned HTTP {exc.code}", exc.code == 429 or exc.code >= 500, delay) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DeliveryError("Provider connection failed or timed out", retry=True) from None
    except (ValueError, TypeError):
        raise DeliveryError("Provider returned an invalid JSON response", retry=True) from None


def send(cfg, event, post=http_post, gateway=None):
    name = event["provider"]
    if name == "sms":
        return send_sms(cfg, event, gateway)
    text = f"{event['title']}\n\n{event['message']}"
    headers = {"Content-Type": "application/json", "User-Agent": "LocalNotify/1.0"}
    if name == "ntfy":
        settings = cfg["ntfy"]
        if not settings["topic"]:
            raise DeliveryError("ntfy topic is not configured")
        if settings["token"]:
            headers["Authorization"] = "Bearer " + settings["token"]
        message = event["message"].encode("utf-8")[:3500].decode("utf-8", errors="ignore")
        payload = {"topic": settings["topic"], "title": event["title"], "message": message, "priority": event["priority"]}
        response = post(settings["server"].rstrip("/") + "/", json.dumps(payload).encode(), headers)
        if not response.get("id"):
            raise DeliveryError("ntfy did not acknowledge the notification", retry=True)
        return str(response["id"])
    if name == "telegram":
        settings = cfg["telegram"]
        if not settings["bot_token"] or not settings["chat_id"]:
            raise DeliveryError("Telegram is not configured")
        payload = {"chat_id": settings["chat_id"], "text": text[:4000], "disable_notification": event["priority"] < 3}
        response = post(f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage", json.dumps(payload).encode(), headers)
        if not response.get("ok"):
            code = response.get("error_code", 400)
            raise DeliveryError(f"Telegram rejected the notification ({code})", code == 429 or code >= 500)
        return str(response["result"]["message_id"])
    if name == "whatsapp":
        settings = cfg["whatsapp"]
        if any(not settings[k] for k in ("account_sid", "auth_token", "from", "to")):
            raise DeliveryError("WhatsApp is not configured")
        auth = base64.b64encode(f"{settings['account_sid']}:{settings['auth_token']}".encode()).decode()
        headers.update({"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
        payload = {"From": settings["from"], "To": settings["to"]}
        if settings["content_sid"]:
            payload.update(ContentSid=settings["content_sid"], ContentVariables=json.dumps({"1": event["title"], "2": event["message"][:1300]}))
        else:
            payload["Body"] = text[:1500]
        response = post(f"https://api.twilio.com/2010-04-01/Accounts/{settings['account_sid']}/Messages.json", urllib.parse.urlencode(payload).encode(), headers)
        if response.get("error_code") or response.get("status") in ("failed", "undelivered") or not response.get("sid"):
            raise DeliveryError("Twilio did not accept the notification")
        return response["sid"]
    raise DeliveryError("Unknown notification provider")
