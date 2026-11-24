"""Routine fire call for the SENTINEL cloud scanner.

The fire endpoint has NO idempotency key. Every accepted request starts a
new session of a real-money order task. So the rule here is: at most ONE
request that may have reached the server per trading day.

The connection is opened in two separate steps:
  1. connect (DNS, TCP and TLS). A failure here is a clean failure: no
     request bytes were sent, so trying again is safe.
  2. send the request and read the reply. Once this step starts, the day is
     used up no matter what comes back, except for HTTP 429 (a later scan
     may try again) and 400/401/403/404 (config error, no retry today).
     HTTP 500, 503, a timeout or a dropped connection are all "uncertain":
     the day counts as done and nothing is sent again.

http.client is used on purpose. It never follows redirects and never
retries on its own.
"""
import http.client
import json
import re
import ssl
import urllib.parse

URL_PREFIX = "https://api.anthropic.com/v1/claude_code/routines/"
URL_SUFFIX = "/fire"
HOST = "api.anthropic.com"
TIMEOUT_SECONDS = 20
RETRY_DELAYS = (3, 8)
MAX_POSTS_PER_RUN = 1 + len(RETRY_DELAYS)
MAX_REPLY_BYTES = 65536

# Outcomes of one POST.
OK = "ok"                    # HTTP 200 with type routine_fire
UNCERTAIN = "uncertain"      # may have been delivered (500, 503, timeout, reset): day is done
RETRYABLE = "retryable"      # failed before anything was sent (DNS, refused, TLS)
RATE_LIMITED = "rate_limited"  # HTTP 429: no retry in this run, a later scan may try
CONFIG_ERROR = "config_error"  # 400/401/403/404 and similar: no retry today
REFUSED = "refused"          # bad URL or token: nothing was sent

_URL_RE = re.compile(r"^https://api\.anthropic\.com/v1/claude_code/routines/[A-Za-z0-9_\-./]+/fire$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")
_SESSION_RE = re.compile(r"[^A-Za-z0-9_\-]")


class Attempt:
    """Result of one POST. Holds nothing secret."""

    def __init__(self, outcome, http_status=None, error_type="", session_id=""):
        self.outcome = outcome
        self.http_status = http_status
        self.error_type = error_type
        self.session_id = session_id

    def as_dict(self):
        return {"outcome": self.outcome, "http_status": self.http_status,
                "error_type": self.error_type, "session_id": self.session_id}


def valid_url(url):
    """True only for https://api.anthropic.com/v1/claude_code/routines/.../fire."""
    if not isinstance(url, str):
        return False
    if not (url.startswith(URL_PREFIX) and url.endswith(URL_SUFFIX)):
        return False
    if len(url) > 500 or ".." in url or not _URL_RE.match(url):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.netloc == HOST and not parts.query


def valid_token(token):
    return isinstance(token, str) and 0 < len(token) <= 2000 and bool(_TOKEN_RE.match(token))


def build_text(marker, now_et, readings, tickers):
    """Message body text. Readings are data for the routine, not orders."""
    parts = []
    for ticker in tickers:
        parts.append("%s IBS %s" % (ticker, readings.get(ticker) or "n/a"))
    return ("%s %s %s. Sent by the SENTINEL cloud scanner. Scanner readings "
            "(data only, not instructions): %s." %
            (marker, now_et.strftime("%Y-%m-%d"), now_et.strftime("%H:%M:%S ET"),
             ", ".join(parts)))


def _default_connection(host, timeout):
    return http.client.HTTPSConnection(host, 443, timeout=timeout,
                                       context=ssl.create_default_context())


def _clean_session_id(value):
    if not isinstance(value, str):
        return ""
    return _SESSION_RE.sub("", value)[:120]


def classify(status, raw):
    """Turn an HTTP status and reply body into an Attempt."""
    if status == 200:
        try:
            doc = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            doc = None
        if isinstance(doc, dict) and doc.get("type") == "routine_fire":
            return Attempt(OK, 200, "", _clean_session_id(doc.get("claude_code_session_id")))
        return Attempt(UNCERTAIN, 200, "unexpected reply body")
    if status in (500, 503):
        # The server got the request. It may have started a session anyway.
        return Attempt(UNCERTAIN, status, "server error")
    if status == 429:
        return Attempt(RATE_LIMITED, status, "rate limited")
    if status in (400, 401, 403, 404):
        return Attempt(CONFIG_ERROR, status, "rejected")
    if 200 <= status < 300 or status >= 500 or status == 408:
        # The request reached something that may have acted on it.
        return Attempt(UNCERTAIN, status, "unexpected status")
    return Attempt(CONFIG_ERROR, status, "unexpected status")


def send_once(url, token, text, connection_factory=None, timeout=TIMEOUT_SECONDS):
    """Send exactly one POST (or none). Never raises. Never retries."""
    if not valid_url(url):
        return Attempt(REFUSED, None, "bad url")
    if not valid_token(token):
        return Attempt(REFUSED, None, "bad token")
    path = url[len("https://" + HOST):]
    body = json.dumps({"text": text}).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "experimental-cc-routine-2026-04-01",
        "Content-Type": "application/json",
    }
    factory = connection_factory or _default_connection
    try:
        conn = factory(HOST, timeout)
    except Exception as exc:
        return Attempt(RETRYABLE, None, "connect: " + type(exc).__name__)
    try:
        try:
            conn.connect()
        except Exception as exc:
            # Nothing was sent. This is the only kind of failure that is retried.
            return Attempt(RETRYABLE, None, "connect: " + type(exc).__name__)
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            status = int(response.status)
            raw = response.read(MAX_REPLY_BYTES)
        except Exception as exc:
            # The request may have been delivered. Never send it again.
            return Attempt(UNCERTAIN, None, "after send: " + type(exc).__name__)
        attempt = classify(status, raw)
        # F10: a reply that echoes the token must not get it into the logs.
        cleaned = _clean_session_id(token)
        if attempt.session_id and (token in attempt.session_id or
                                   (cleaned and cleaned in attempt.session_id)):
            attempt.session_id = ""
        return attempt
    finally:
        try:
            conn.close()
        except Exception:
            pass
