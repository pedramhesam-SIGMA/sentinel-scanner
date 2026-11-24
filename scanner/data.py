"""Market data fetch and parsing for the SENTINEL cloud scanner.

Primary source: Yahoo Finance chart endpoint. Optional fallback: Alpaca
(only when ALPACA_KEY_ID and ALPACA_SECRET_KEY are set).

Everything that comes back from the web is treated as data only. Nothing in
a response is ever evaluated or executed.
"""
import json
import re
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

from . import rules

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_PATH = "/v8/finance/chart/{ticker}?interval=5m&range=5d&includePrePost=false"
ALPACA_URL = "https://data.alpaca.markets/v2/stocks/{ticker}/bars"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HTTP_TIMEOUT = 15
ATTEMPTS_PER_HOST = 2
BACKOFF_SECONDS = 1
TOTAL_BUDGET_SECONDS = 75
MAX_BYTES = 8 * 1024 * 1024
TICKER_RE = re.compile(r"^[A-Z]{1,6}$")
PRICE_QUANT = Decimal("0.0001")


class DataError(Exception):
    """Raised when a response cannot be fetched or parsed."""


def _dec(value):
    """Number from JSON -> Decimal rounded to 4 places, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    try:
        return dec.quantize(PRICE_QUANT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        return None   # too large to be a price: the caller drops the bar


def _loads(raw):
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise DataError("response is not JSON") from exc


def _normalize(raw_bars, tz):
    """Sort, de-duplicate and keep regular session 5-minute bars only.

    raw_bars: iterable of (start aware datetime, open, high, low, close).
    A later duplicate of the same start time replaces the earlier one.
    Bars that do not start on a 5-minute mark are dropped (Yahoo appends a
    live partial bar stamped with the current time).
    """
    by_start = {}
    for start, o, h, l, c in raw_bars:
        if None in (o, h, l, c):
            continue
        if min(o, h, l, c) <= 0 or h < l or h < max(o, c) or l > min(o, c):
            continue
        start = start.astimezone(tz)
        if start.second or start.microsecond or start.minute % 5:
            continue
        if not (rules.SESSION_OPEN <= start.time() < rules.SESSION_CLOSE):
            continue
        by_start[start] = {"start": start, "open": o, "high": h, "low": l, "close": c}
    return [by_start[k] for k in sorted(by_start)]


def parse_yahoo(raw, tz_name="America/New_York", ticker=None):
    """Parse a Yahoo chart response into bars plus the last price.

    If `ticker` is given and the response names a different symbol, the
    response is refused.
    """
    tz = ZoneInfo(tz_name)
    doc = _loads(raw)
    try:
        result = doc["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        cols = [quote[k] for k in ("open", "high", "low", "close")]
        meta = result.get("meta") or {}
    except (KeyError, IndexError, TypeError) as exc:
        raise DataError("yahoo response has an unexpected shape") from exc
    if not isinstance(stamps, list) or any(not isinstance(c, list) for c in cols):
        raise DataError("yahoo response has an unexpected shape")
    symbol = meta.get("symbol") if isinstance(meta, dict) else None
    if ticker is not None and symbol is not None and symbol != ticker:
        raise DataError("yahoo response is for another symbol")
    if any(len(c) != len(stamps) for c in cols):
        raise DataError("yahoo arrays differ in length")
    raw_bars = []
    for i, stamp in enumerate(stamps):
        if isinstance(stamp, bool) or not isinstance(stamp, (int, Decimal)):
            continue
        try:
            start = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        raw_bars.append((start,) + tuple(_dec(c[i]) for c in cols))
    last_price = _dec(meta.get("regularMarketPrice")) if isinstance(meta, dict) else None
    last_time = None
    stamp = meta.get("regularMarketTime") if isinstance(meta, dict) else None
    if isinstance(stamp, (int, Decimal)) and not isinstance(stamp, bool):
        try:
            last_time = datetime.fromtimestamp(int(stamp), tz=timezone.utc).astimezone(tz)
        except (OverflowError, OSError, ValueError):
            last_time = None
    if last_price is not None and last_price <= 0:
        last_price = None
    if last_price is None or last_time is None:
        last_price, last_time = None, None
    bars = _normalize(raw_bars, tz)
    if not bars:
        raise DataError("yahoo response has no usable bars")
    return {"source": "yahoo", "bars": bars,
            "last_price": last_price, "last_price_time": last_time}


def parse_alpaca(raw, tz_name="America/New_York"):
    """Parse an Alpaca bars response (or a list of bar dicts)."""
    tz = ZoneInfo(tz_name)
    doc = _loads(raw)
    items = doc.get("bars") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise DataError("alpaca response has an unexpected shape")
    raw_bars = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("t"), str):
            continue
        try:
            start = datetime.fromisoformat(item["t"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if start.tzinfo is None:
            continue
        raw_bars.append((start,) + tuple(_dec(item.get(k)) for k in "ohlc"))
    bars = _normalize(raw_bars, tz)
    if not bars:
        raise DataError("alpaca response has no usable bars")
    newest = bars[-1]
    return {"source": "alpaca", "bars": bars, "last_price": newest["close"],
            "last_price_time": newest["start"] + rules.BAR}


# ------------------------------------------------------------------- fetch

def _http_get(url, headers, timeout, opener):
    request = urllib.request.Request(url, headers=headers, method="GET")
    with opener(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise DataError("http status %s" % status)
        return response.read(MAX_BYTES)


def _short_error(exc):
    """Error text that is safe to log: type name and HTTP status only."""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTPError %s" % exc.code
    if isinstance(exc, DataError):
        return "DataError: %s" % exc
    return type(exc).__name__


def fetch_yahoo(ticker, tz_name, opener, sleep, deadline, clock):
    last_error = "no attempt made"
    for host in YAHOO_HOSTS:
        for attempt in range(ATTEMPTS_PER_HOST):
            remaining = deadline - clock()
            if remaining <= 1:
                raise DataError("time budget used up (%s)" % last_error)
            url = "https://" + host + YAHOO_PATH.format(ticker=ticker)
            try:
                raw = _http_get(url, {"User-Agent": USER_AGENT, "Accept": "application/json"},
                                min(HTTP_TIMEOUT, remaining), opener)
                return parse_yahoo(raw, tz_name, ticker)
            except Exception as exc:  # network and parse errors are both retried
                last_error = _short_error(exc)
            if attempt + 1 < ATTEMPTS_PER_HOST:
                sleep(BACKOFF_SECONDS)
    raise DataError("yahoo failed (%s)" % last_error)


def fetch_alpaca(ticker, cfg, env, now, opener, deadline, clock):
    start_day = now.date()
    for _ in range(5):
        start_day = rules.prev_trading_day(start_day, cfg)
    start = rules.at(start_day, "00:00:00").astimezone(timezone.utc)
    headers = {"APCA-API-KEY-ID": env["ALPACA_KEY_ID"],
               "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"],
               "Accept": "application/json"}
    items, token = [], None
    for _ in range(4):
        remaining = deadline - clock()
        if remaining <= 1:
            raise DataError("time budget used up")
        query = {"timeframe": "5Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "feed": "iex", "limit": "1000"}
        if token:
            query["page_token"] = token
        url = ALPACA_URL.format(ticker=ticker) + "?" + urllib.parse.urlencode(query)
        doc = _loads(_http_get(url, headers, min(HTTP_TIMEOUT, remaining), opener))
        if not isinstance(doc, dict) or not isinstance(doc.get("bars") or [], list):
            raise DataError("alpaca response has an unexpected shape")
        items.extend(doc.get("bars") or [])
        token = doc.get("next_page_token")
        if not isinstance(token, str) or not token:
            break
    return parse_alpaca(items, cfg["timezone"])


def fetch_all(tickers, cfg, env, now, opener=None, sleep=_time.sleep, clock=_time.monotonic):
    """Fetch every ticker. Returns (data by ticker, error text by ticker).

    One ticker failing never stops the others. The whole fetch is held to a
    fixed time budget so a slow data host cannot starve the rest of the run.
    """
    opener = opener or urllib.request.urlopen
    deadline = clock() + TOTAL_BUDGET_SECONDS
    use_alpaca = bool(env.get("ALPACA_KEY_ID")) and bool(env.get("ALPACA_SECRET_KEY"))
    data, errors = {}, {}
    for ticker in tickers:
        if not TICKER_RE.match(ticker):
            errors[ticker] = "bad ticker symbol"
            continue
        try:
            data[ticker] = fetch_yahoo(ticker, cfg["timezone"], opener, sleep, deadline, clock)
            continue
        except Exception as exc:
            errors[ticker] = _short_error(exc)
        if use_alpaca:
            try:
                data[ticker] = fetch_alpaca(ticker, cfg, env, now, opener, deadline, clock)
                del errors[ticker]
            except Exception as exc:
                errors[ticker] += "; alpaca: " + _short_error(exc)
    return data, errors


def load_fixture_dir(path, tickers, cfg):
    """Offline data: read Yahoo-shaped files named TICKER.json from a folder."""
    import os
    data, errors = {}, {}
    for ticker in tickers:
        try:
            with open(os.path.join(path, ticker + ".json"), "rb") as handle:
                data[ticker] = parse_yahoo(handle.read(), cfg["timezone"], ticker)
        except Exception as exc:
            errors[ticker] = _short_error(exc)
    return data, errors
