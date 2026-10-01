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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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


def send(cfg, event, post=http_post):
    name = event["provider"]
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
