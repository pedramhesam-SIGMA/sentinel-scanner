"""Pure trading rules for the SENTINEL cloud scanner.

No I/O and no clock reads in this module. Every function gets the time it
needs as an argument. All price math uses decimal.Decimal.

A bar is a dict: {"start": aware datetime, "open", "high", "low", "close"}.
"start" is the START of the 5-minute bar.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BAR = timedelta(minutes=5)
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)
# Prior high needs at least 95% of the previous day's expected bars.
MIN_PREV_BARS_FULL_DAY = 75   # of 78
MIN_PREV_BARS_HALF_DAY = 40   # of 42 (half day session is 09:30 to 13:00 ET)
CENT = Decimal("0.01")


# ---------------------------------------------------------------- calendar

def as_date(value):
    """Accept a date or an ISO date string and return a date."""
    if isinstance(value, datetime):
        return value.astimezone(ET).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def parse_clock(text):
    """'15:40' or '15:40:00' -> datetime.time."""
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


def at(day, clock):
    """ET aware datetime for a date and a clock string or time."""
    if isinstance(clock, str):
        clock = parse_clock(clock)
    return datetime.combine(as_date(day), clock, tzinfo=ET)


def is_trading_day(day, cfg):
    day = as_date(day)
    return day.weekday() < 5 and day.isoformat() not in cfg["holidays"]


def is_half_day(day, cfg):
    day = as_date(day)
    return is_trading_day(day, cfg) and day.isoformat() in cfg["half_days"]


def prev_trading_day(day, cfg):
    """The last trading day strictly before `day`. Half days count."""
    day = as_date(day) - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(day, cfg):
            return day
        day -= timedelta(days=1)
    raise ValueError("no trading day found in the 30 days before the date")


def trading_days_after(entry_date, today, cfg):
    """Number of trading days after entry_date, up to and including today.

    Half days count. Returns 0 when today is the entry day or earlier.
    """
    entry_date = as_date(entry_date)
    today = as_date(today)
    count = 0
    day = entry_date + timedelta(days=1)
    while day <= today:
        if is_trading_day(day, cfg):
            count += 1
        day += timedelta(days=1)
    return count


# -------------------------------------------------------------------- bars

def session_bars(bars, day):
    """Bars of that ET date inside the regular session, sorted by start."""
    day = as_date(day)
    out = []
    for bar in bars:
        start = bar["start"].astimezone(ET)
        if start.date() == day and SESSION_OPEN <= start.time() < SESSION_CLOSE:
            out.append(bar)
    out.sort(key=lambda b: b["start"])
    return out


def completed_bars(bars, now):
    """Bars that are finished at time `now` (now >= start + 5 minutes)."""
    return [b for b in bars if b["start"] + BAR <= now]


def _high_low(bars):
    if not bars:
        return None, None
    return max(b["high"] for b in bars), min(b["low"] for b in bars)


# ------------------------------------------------------- S2-E entry signal

def checkpoint_signal(bars_today, checkpoint_dt, cfg):
    """S2-E signal at one checkpoint.

    Uses ONLY bars of the checkpoint's own date that start at or after 09:30
    and that are finished by the checkpoint (start + 5 min <= checkpoint).
    Bars that start at or after the checkpoint are never read, so the signal
    cannot look ahead no matter what the caller passes in.
    """
    s2e = cfg["s2e"]
    cp = checkpoint_dt.astimezone(ET)
    day_open = datetime.combine(cp.date(), SESSION_OPEN, tzinfo=ET)
    used = [b for b in bars_today
            if b["start"] >= day_open and b["start"] + BAR <= cp]
    out = {"H": None, "L": None, "P": None, "R": None, "range_pct": None,
           "enter": False, "skip_reason": None, "n_bars": len(used)}
    signal_start = cp - BAR
    signal = [b for b in used if b["start"] == signal_start]
    if not signal:
        out["skip_reason"] = "missing signal bar"
        return out
    high, low = _high_low(used)
    price = signal[-1]["close"]
    out.update(H=high, L=low, P=price)
    if high == low:
        out["skip_reason"] = "H equals L"
        return out
    if low <= 0:
        out["skip_reason"] = "bad data"
        return out
    r_value = (price - low) / (high - low)
    range_pct = (high - low) / low
    out.update(R=r_value, range_pct=range_pct)
    if not r_value < Decimal(s2e["r_entry_below"]):
        out["skip_reason"] = "R not below " + s2e["r_entry_below"]
        return out
    if not range_pct >= Decimal(s2e["range_floor"]):
        out["skip_reason"] = "range below floor"
        return out
    out["enter"] = True
    return out


# ------------------------------------------------------------------- exits

def _ibs_parts(bars_today, last_price):
    """H, L, IBS with the last price folded into the day's range."""
    high, low = _high_low(bars_today)
    if high is None:
        high, low = last_price, last_price
    high = max(high, last_price)
    low = min(low, last_price)
    ibs = None
    if high != low:
        ibs = (last_price - low) / (high - low)
    return high, low, ibs


def prior_high(bars_prev_day, today, cfg):
    """Highest high of the previous trading day's regular session, or None.

    Half days are included. The day must have at least 95% of its expected
    bars: 75 of 78 on a full day, 40 of 42 on a half day. Bars of any other
    date, and bars after 13:00 on a half day, are not counted.
    """
    prev_day = prev_trading_day(today, cfg)
    bars = session_bars(bars_prev_day, prev_day)
    needed = MIN_PREV_BARS_FULL_DAY
    if is_half_day(prev_day, cfg):
        bars = [b for b in bars if b["start"].astimezone(ET).time() < HALF_DAY_CLOSE]
        needed = MIN_PREV_BARS_HALF_DAY
    if len(bars) < needed:
        return None
    return max(b["high"] for b in bars)


def exit_decision(bars_today, bars_prev_day, last_price, entry_date, today, cfg):
    """SELL or HOLD for one open paper position.

    bars_today must hold only COMPLETED bars of today (the caller filters).
    bars_prev_day holds the previous trading day's regular session bars.

    SELL if P > PH, or IBS > 0.50, or it is the 5th or a later trading day
    after the entry day. Never on the entry day. If H equals L, nothing but
    the time exit can sell. If PH is missing, only the IBS and time exits
    apply. The time exit needs only the price.
    """
    s2e = cfg["s2e"]
    entry_date = as_date(entry_date)
    today = as_date(today)
    day_count = trading_days_after(entry_date, today, cfg)
    out = {"action": "HOLD", "reason": "", "IBS": None, "PH": None,
           "H": None, "L": None, "P": last_price, "day_count": day_count}
    if today <= entry_date:
        out["reason"] = "entry day"
        return out
    if last_price is None:
        out["reason"] = "no price"
        return out
    high, low, ibs = _ibs_parts(bars_today, last_price)
    ph = prior_high(bars_prev_day, today, cfg)
    out.update(H=high, L=low, IBS=ibs, PH=ph)
    if ibs is not None:
        if ph is not None and last_price > ph:
            out.update(action="SELL", reason="above prior high")
            return out
        if ibs > Decimal(s2e["ibs_exit_above"]):
            out.update(action="SELL", reason="IBS above " + s2e["ibs_exit_above"])
            return out
    if day_count >= int(s2e["max_hold_days"]):
        out.update(action="SELL", reason="time exit")
        return out
    if ibs is None:
        out["reason"] = "H equals L"
    elif ph is None:
        out["reason"] = "no exit condition (prior high missing)"
    else:
        out["reason"] = "no exit condition"
    return out


def s2_crosscheck(bars_today, last_price):
    """IBS reading for the plain S2 rule (signal when IBS < 0.20)."""
    if last_price is None:
        return {"H": None, "L": None, "P": None, "IBS": None, "signal": False}
    high, low, ibs = _ibs_parts(bars_today, last_price)
    return {"H": high, "L": low, "P": last_price, "IBS": ibs,
            "signal": ibs is not None and ibs < Decimal("0.20")}


# ------------------------------------------------------------------- money

def net_dollars(entry_price, exit_price, cfg):
    """notional * (exit/entry - 1 - cost_bp/10000), not rounded."""
    s2e = cfg["s2e"]
    cost = Decimal(s2e["cost_bp"]) / Decimal(10000)
    return Decimal(s2e["notional"]) * (exit_price / entry_price - 1 - cost)


def round_cents(value):
    """Round to cents, half to even. Used for the csv only."""
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)
