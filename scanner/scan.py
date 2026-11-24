"""SENTINEL cloud scanner entry point.

Run with:  python -m scanner.scan
Options:   --dry-run   no fire, no state write, prints decisions
           --now ISO   override the clock (fire is forced off unless
                       --allow-fire is also given)

This program never places an order and holds no broker credentials. Its only
outside effect, besides reading public price data, is at most one accepted
POST per trading day to the routine fire endpoint.
"""
import argparse
import csv
import io
import json
import os
import shlex
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import data as data_mod
from . import fire as fire_mod
from . import rules

EVENT_COLUMNS = ["seq", "time_et", "date", "event", "ticker", "checkpoint", "H", "L", "P",
                 "R", "IBS", "range_pct", "decision", "reason", "detail"]
TRADE_COLUMNS = ["ticker", "entry_date", "entry_time", "entry_price", "checkpoint",
                 "exit_date", "exit_time", "exit_price", "reason", "day_count",
                 "net_dollars", "both_flag"]
LAST_PRICE_MAX_AGE_SECONDS = 180
LAST_PRICE_MAX_GAP = Decimal("0.05")   # last price more than 5% from the newest bar close
SCAN_START = "09:30:00"
SCAN_END = "16:05:59"
LOOKBACK_DAYS = 10
KEEP_DAYS = 60
HEARTBEAT_SECONDS = 3600
PERSIST_TIMEOUT_SECONDS = 90
FIRE_DONE = ("ok", "uncertain")


class StateError(Exception):
    pass


def fmt(value, places=4):
    """Decimal -> plain text with a fixed number of places. None -> ''."""
    if value is None:
        return ""
    return format(value.quantize(Decimal(1).scaleb(-places)), "f")


def load_config(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ------------------------------------------------------------------- store

class Store:
    """State file, csv logs and heartbeat. All writes go through here."""

    def __init__(self, root, dry_run, out):
        self.root = root
        self.dry = dry_run
        self.out = out
        self.dir = os.path.join(root, "state")
        self.state_path = os.path.join(self.dir, "state.json")
        self.events_path = os.path.join(self.dir, "events.csv")
        self.trades_path = os.path.join(self.dir, "paper_trades.csv")
        self.heartbeat_path = os.path.join(self.dir, "heartbeat.json")
        self.state = None
        self._lock_handle = None

    def say(self, text):
        print(text, file=self.out)

    # -- locking: stops two scans on the same folder at the same moment
    def lock(self):
        if self.dry:
            return True
        os.makedirs(self.dir, exist_ok=True)
        try:
            import fcntl
        except ImportError:
            return True
        self._lock_handle = open(os.path.join(self.dir, ".lock"), "a")
        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_handle.close()
            self._lock_handle = None
            return False
        return True

    def unlock(self):
        if self._lock_handle is not None:
            self._lock_handle.close()
            self._lock_handle = None

    # -- state
    def load(self, now):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                if not isinstance(state, dict) or not isinstance(state.get("fire"), dict) \
                        or not isinstance(state.get("days"), dict) \
                        or not isinstance(state.get("positions"), dict):
                    raise ValueError("wrong shape")
            except (ValueError, OSError) as exc:
                raise StateError("state.json cannot be read: %s" % type(exc).__name__)
        else:
            # A fresh state must not reuse row numbers already in events.csv.
            state = {"version": 1, "installed_at": now.isoformat(), "seq": self._max_seq(),
                     "positions": {}, "days": {}, "fire": {},
                     "totals": {"closed_trades": 0, "wins": 0, "net_dollars": "0"},
                     "fire_config_error": False,
                     "outbox": {"events": [], "trades": []}}
        state.setdefault("outbox", {"events": [], "trades": []})
        state.setdefault("seq", 0)
        state.setdefault("totals", {"closed_trades": 0, "wins": 0, "net_dollars": "0"})
        state.setdefault("installed_at", now.isoformat())
        state.setdefault("fire_config_error", False)
        self.state = state
        return state

    def _max_seq(self):
        top = 0
        if os.path.exists(self.events_path):
            with open(self.events_path, "r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        top = max(top, int(row.get("seq") or 0))
                    except ValueError:
                        continue
        return top

    def _atomic_write(self, path, text):
        os.makedirs(self.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _write_state_if_changed(self):
        text = json.dumps(self.state, indent=1, sort_keys=True) + "\n"
        old = None
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as handle:
                old = handle.read()
        if text != old:
            self._atomic_write(self.state_path, text)

    def _append_rows(self, path, columns, rows, key):
        """Append rows that are not in the file yet (matched by key columns)."""
        seen = set()
        exists = os.path.exists(path)
        if exists:
            with open(path, "r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    seen.add(tuple(row.get(k, "") for k in key))
        fresh = [r for r in rows if tuple(str(r.get(k, "")) for k in key) not in seen]
        if not fresh:
            return
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            if not exists:
                writer.writeheader()
            for row in fresh:
                writer.writerow({c: row.get(c, "") for c in columns})
            handle.flush()
            os.fsync(handle.fileno())

    def commit(self):
        """Save state, then move queued csv rows to the csv files.

        Queued rows live inside state.json until they are in the csv files,
        so a crash between the two steps cannot lose or double a row.
        """
        if self.dry:
            return
        box = self.state["outbox"]
        if box["events"] or box["trades"]:
            self._write_state_if_changed()
            self._append_rows(self.events_path, EVENT_COLUMNS, box["events"], ["seq"])
            self._append_rows(self.trades_path, TRADE_COLUMNS, box["trades"],
                              ["ticker", "entry_date", "checkpoint"])
            self.state["outbox"] = {"events": [], "trades": []}
        self._write_state_if_changed()

    # -- logs
    def event(self, now, kind, ticker="", checkpoint="", decision="", reason="",
              detail="", **nums):
        self.state["seq"] += 1
        row = {"seq": str(self.state["seq"]), "time_et": now.strftime("%Y-%m-%d %H:%M:%S"),
               "date": now.strftime("%Y-%m-%d"), "event": kind, "ticker": ticker,
               "checkpoint": checkpoint, "decision": decision, "reason": reason,
               "detail": detail}
        for name in ("H", "L", "P"):
            row[name] = fmt(nums.get(name), 4)
        for name in ("R", "IBS", "range_pct"):
            row[name] = fmt(nums.get(name), 6)
        self.state["outbox"]["events"].append(row)
        shown = " ".join("%s=%s" % (k, row[k]) for k in EVENT_COLUMNS[3:] if row[k])
        self.say("EVENT " + shown)

    def trade(self, row):
        self.state["outbox"]["trades"].append(row)
        self.say("TRADE " + " ".join("%s=%s" % (k, row[k]) for k in TRADE_COLUMNS if row[k]))

    def fired_in_events(self, date_text):
        """True if events.csv already shows an accepted or uncertain fire today.

        Second line of defense in case state.json was lost or replaced.
        """
        rows = list(self.state["outbox"]["events"])
        if os.path.exists(self.events_path):
            with open(self.events_path, "r", encoding="utf-8", newline="") as handle:
                rows.extend(csv.DictReader(handle))
        for row in rows:
            if row.get("event") != "fire attempt" or row.get("decision") not in FIRE_DONE:
                continue
            detail = row.get("detail") or ""
            # A claim closed on a later day carries the day it belongs to.
            fire_date = detail[len("fire_date="):] if detail.startswith("fire_date=") \
                else row.get("date")
            if fire_date == date_text:
                return True
        return False

    # -- heartbeat
    def heartbeat(self, now, status, note):
        fire_today = self.state["fire"].get(now.strftime("%Y-%m-%d"), {})
        beat = {"last_scan": now.isoformat(), "status": status, "note": note,
                "open_positions": sorted(self.state["positions"]),
                "fire_today": fire_today.get("result", "none"),
                "closed_trades": self.state["totals"]["closed_trades"]}
        self.say("HEARTBEAT status=%s note=%s" % (status, note))
        if self.dry:
            return
        old = None
        try:
            with open(self.heartbeat_path, "r", encoding="utf-8") as handle:
                old = json.load(handle)
        except (OSError, ValueError):
            old = None
        if isinstance(old, dict):
            same = all(old.get(k) == beat[k] for k in beat if k != "last_scan")
            try:
                age = (now - datetime.fromisoformat(old["last_scan"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = None
            if same and age is not None and 0 <= age < HEARTBEAT_SECONDS:
                return
        self._atomic_write(self.heartbeat_path, json.dumps(beat, indent=1, sort_keys=True) + "\n")


# ----------------------------------------------------------------- helpers

def day_rec(state, date_text):
    return state["days"].setdefault(date_text, {})


def once(state, date_text, key):
    """True the first time a key is seen on a date. Used for one-time events."""
    notes = day_rec(state, date_text).setdefault("notes", [])
    if key in notes:
        return False
    notes.append(key)
    return True


class View:
    """What the scanner may know about one ticker at time `now`."""

    def __init__(self, parsed, now, today, cfg, price_asof=None):
        done = rules.completed_bars(parsed["bars"], now)
        self.source = parsed.get("source", "")
        self.today = rules.session_bars(done, today)
        self.prev = rules.session_bars(done, rules.prev_trading_day(today, cfg))
        self.newest = self.today[-1] if self.today else None
        limit = int(cfg["data"]["bar_freshness_seconds"])
        self.fresh = self.newest is not None and \
            (now - self.newest["start"]).total_seconds() <= limit
        price, when = parsed.get("last_price"), parsed.get("last_price_time")
        self.last_price = None
        self.price_note = ""
        if price is not None and when is not None:
            age = ((price_asof or now) - when).total_seconds()
            if 0 <= age < LAST_PRICE_MAX_AGE_SECONDS:
                self.last_price = price
        # A last price far from the newest finished bar is treated as bad data.
        if self.last_price is not None and done:
            ref = done[-1]["close"]
            if abs(self.last_price - ref) > ref * LAST_PRICE_MAX_GAP:
                self.last_price = None
                self.price_note = "last price rejected"

    def price(self):
        """(price, source). The last trade if fresh, else newest finished bar close."""
        if self.last_price is not None:
            return self.last_price, "last"
        if self.fresh:
            return self.newest["close"], "bar"
        return None, ""


def resolve_pending_fires(store, now):
    """A fire claim with no recorded result means an earlier run died mid-call.

    The request may have been delivered, so the day is closed for firing.
    """
    for date_text, rec in sorted(store.state["fire"].items()):
        if rec.get("pending"):
            rec["pending"] = False
            rec["ok"] = True
            rec["result"] = "uncertain"
            rec["error_type"] = "earlier run ended before the result was recorded"
            store.event(now, "fire attempt", decision="uncertain",
                        reason="earlier run ended before the result was recorded",
                        detail="fire_date=%s" % date_text)


def prune(state, today):
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for name in ("days", "fire"):
        for key in [k for k in state[name] if k < cutoff]:
            del state[name][key]


# ------------------------------------------------------------- paper engine

def log_missed(store, cfg, now, today):
    """Record checkpoints and exit checks whose window passed with no scan."""
    state = store.state
    s2e = cfg["s2e"]
    tickers = cfg["tickers"]
    window = timedelta(seconds=int(s2e["checkpoint_window_seconds"]))
    try:
        installed = datetime.fromisoformat(state["installed_at"])
    except (ValueError, TypeError):
        installed = now
    day = today - timedelta(days=LOOKBACK_DAYS)
    while day <= today:
        text = day.isoformat()
        if rules.is_trading_day(day, cfg) and not rules.is_half_day(day, cfg):
            for cp in s2e["checkpoints"]:
                end = rules.at(day, cp) + window
                if not (installed < end < now):
                    continue
                done = state["days"].get(text, {}).get("checkpoints", {}).get(cp, {})
                left = [t for t in tickers if t not in done]
                if left and once(state, text, "missed_cp:" + cp):
                    store.event(now, "missed checkpoint", checkpoint=cp, decision="skipped",
                                reason="no usable scan in the window",
                                detail="date=%s tickers=%s" % (text, "/".join(left)))
            end = rules.at(day, s2e["exit_window"][1])
            if installed < end < now:
                done = state["days"].get(text, {}).get("exit", {})
                left = [t for t in tickers if t not in done]
                if left and once(state, text, "missed_exit"):
                    held = [t for t in left if t in state["positions"]
                            and state["positions"][t]["entry_date"] < text]
                    store.event(now, "MISSED EXIT CHECK", decision="skipped",
                                reason="no usable scan in the exit window",
                                detail="date=%s tickers=%s held=%s" %
                                (text, "/".join(left), "/".join(held) or "none"))
        day += timedelta(days=1)


def run_checkpoints(store, cfg, now, today, views):
    state = store.state
    s2e = cfg["s2e"]
    text = today.isoformat()
    window = timedelta(seconds=int(s2e["checkpoint_window_seconds"]))
    for cp in s2e["checkpoints"]:
        cp_dt = rules.at(today, cp)
        if not (cp_dt <= now <= cp_dt + window):
            continue
        for ticker in cfg["tickers"]:
            day = day_rec(state, text)
            if ticker in day.get("checkpoints", {}).get(cp, {}):
                continue
            view = views.get(ticker)
            if view is None:
                continue  # fetch failed: an error event was written; next scan may retry
            if not view.fresh:
                if once(state, text, "stale_cp:%s:%s" % (cp, ticker)):
                    store.event(now, "checkpoint", ticker, cp, decision="skipped",
                                reason="stale data")
                continue
            sig = rules.checkpoint_signal(view.today, cp_dt, cfg)
            decision, reason, detail = "no entry", sig["skip_reason"] or "", ""
            if sig["enter"]:
                if ticker in state["positions"]:
                    reason = "signal, but a position is open"
                elif ticker in day.get("entries", []):
                    reason = "signal, but already entered today"
                else:
                    price, source = view.price()
                    decision, reason = "ENTER", "R below %s" % s2e["r_entry_below"]
                    flag = "entry price from bar" if source == "bar" else ""
                    if view.price_note:
                        flag = (flag + "; " + view.price_note).strip("; ")
                    detail = "entry_price=%s%s" % (fmt(price), "; " + flag if flag else "")
                    state["positions"][ticker] = {
                        "ticker": ticker, "entry_date": text,
                        "entry_time": now.strftime("%H:%M:%S"),
                        "entry_price": fmt(price), "checkpoint": cp, "flag": flag,
                        "H": fmt(sig["H"]), "L": fmt(sig["L"]), "P": fmt(sig["P"]),
                        "R": fmt(sig["R"], 6), "range_pct": fmt(sig["range_pct"], 6)}
                    day.setdefault("entries", []).append(ticker)
            day.setdefault("checkpoints", {}).setdefault(cp, {})[ticker] = decision
            store.event(now, "checkpoint", ticker, cp, decision=decision, reason=reason,
                        detail=(detail + " bars=%d" % sig["n_bars"]).strip(),
                        H=sig["H"], L=sig["L"], P=sig["P"], R=sig["R"],
                        range_pct=sig["range_pct"])


def close_position(store, cfg, now, pos, price, verdict, flag):
    state = store.state
    entry_price = Decimal(pos["entry_price"])
    net = rules.net_dollars(entry_price, price, cfg)
    store.trade({"ticker": pos["ticker"], "entry_date": pos["entry_date"],
                 "entry_time": pos["entry_time"], "entry_price": pos["entry_price"],
                 "checkpoint": pos["checkpoint"], "exit_date": now.strftime("%Y-%m-%d"),
                 "exit_time": now.strftime("%H:%M:%S"), "exit_price": fmt(price),
                 "reason": verdict["reason"] + ("; " + flag if flag else ""),
                 "day_count": str(verdict["day_count"]),
                 "net_dollars": str(rules.round_cents(net)), "both_flag": ""})
    totals = state["totals"]
    totals["closed_trades"] += 1
    totals["wins"] += 1 if net > 0 else 0
    totals["net_dollars"] = str(Decimal(totals["net_dollars"]) + net)
    del state["positions"][pos["ticker"]]


def run_exit_window(store, cfg, now, today, views):
    state = store.state
    s2e = cfg["s2e"]
    text = today.isoformat()
    if not (rules.at(today, s2e["exit_window"][0]) <= now <= rules.at(today, s2e["exit_window"][1])):
        return
    for ticker in cfg["tickers"]:
        day = day_rec(state, text)
        if ticker in day.get("exit", {}):
            continue
        view = views.get(ticker)
        if view is None:
            continue
        pos = state["positions"].get(ticker)
        price, source = view.price()
        if not view.fresh:
            # Bars are stale. Only the time exit may run, and only on a fresh last price.
            if pos is not None and view.last_price is not None:
                verdict = rules.exit_decision([], [], view.last_price, pos["entry_date"],
                                              today, cfg)
                if verdict["action"] == "SELL":
                    store.event(now, "exit check", ticker, decision="SELL",
                                reason=verdict["reason"],
                                detail="stale bars; day_count=%d bars=%d prev_bars=%d" %
                                (verdict["day_count"], len(view.today), len(view.prev)),
                                P=view.last_price)
                    close_position(store, cfg, now, pos, view.last_price, verdict, "stale bars")
                    day.setdefault("exit", {})[ticker] = "SELL"
                    continue
            if once(state, text, "stale_exit:" + ticker):
                store.event(now, "exit check", ticker, decision="skipped", reason="stale data",
                            detail="bars=%d prev_bars=%d%s" %
                            (len(view.today), len(view.prev),
                             "; " + view.price_note if view.price_note else ""))
            continue
        flag = "exit price from bar" if source == "bar" else ""
        if view.price_note:
            flag = (flag + "; " + view.price_note).strip("; ")
        check = rules.s2_crosscheck(view.today, price)
        store.event(now, "s2 crosscheck", ticker,
                    decision="SIGNAL" if check["signal"] else "no signal",
                    reason="IBS below 0.20" if check["signal"] else "",
                    detail=("price from bar" if source == "bar" else "") +
                    ("; " + view.price_note if view.price_note else ""),
                    H=check["H"], L=check["L"], P=check["P"], IBS=check["IBS"])
        outcome = "checked"
        if pos is not None:
            verdict = rules.exit_decision(view.today, view.prev, price, pos["entry_date"],
                                          today, cfg)
            store.event(now, "exit check", ticker, decision=verdict["action"],
                        reason=verdict["reason"],
                        detail="day_count=%d PH=%s bars=%d prev_bars=%d%s" % (
                            verdict["day_count"], fmt(verdict["PH"]) or "missing",
                            len(view.today), len(view.prev), "; " + flag if flag else ""),
                        H=verdict["H"], L=verdict["L"], P=price, IBS=verdict["IBS"])
            outcome = verdict["action"]
            if verdict["action"] == "SELL":
                close_position(store, cfg, now, pos, price, verdict, flag)
        day.setdefault("exit", {})[ticker] = outcome


# -------------------------------------------------------------------- fire

SECRET_ENV_NAMES = ("ROUTINE_FIRE_URL", "ROUTINE_FIRE_TOKEN", "ALPACA_KEY_ID",
                    "ALPACA_SECRET_KEY")


def default_persist(command, root):
    """Run the state persist command (no shell). True when it exits 0.

    The command's error output goes to the job log so a failed push can be
    diagnosed. The fire and data secrets are removed from its environment
    first, so the command cannot print them.
    """
    child_env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_NAMES}
    try:
        done = subprocess.run(shlex.split(command), cwd=root, timeout=PERSIST_TIMEOUT_SECONDS,
                              stdout=subprocess.DEVNULL, stderr=None, env=child_env)
    except Exception as exc:
        print("persist command could not run: %s" % type(exc).__name__, file=sys.stderr)
        return False
    return done.returncode == 0


def run_fire(store, cfg, now, today, views, env, args, sender, persist, sleep):
    state = store.state
    fcfg = cfg["fire"]
    text = today.isoformat()
    if not fcfg.get("enabled"):
        return
    if args.now and not args.allow_fire:
        store.say("FIRE off: --now was given without --allow-fire")
        return
    url = (env.get("ROUTINE_FIRE_URL") or "").strip()
    token = (env.get("ROUTINE_FIRE_TOKEN") or "").strip()
    in_window = rules.at(today, fcfg["window"][0]) <= now <= rules.at(today, fcfg["window"][1])
    if store.dry:
        store.say("FIRE dry run: configured=%s in_window=%s (nothing is sent)" %
                  (bool(url and token), in_window))
        return
    if not url or not token:
        if once(state, text, "fire_not_configured"):
            store.event(now, "fire not configured", decision="skipped",
                        reason="ROUTINE_FIRE_URL or ROUTINE_FIRE_TOKEN is not set")
        return
    if not in_window:
        return
    rec = state["fire"].get(text, {})
    if rec.get("ok") or rec.get("pending") or rec.get("no_retry_today"):
        return
    max_attempts = int(fcfg["max_attempts_per_day"])
    if int(rec.get("attempts", 0)) >= max_attempts:
        return
    if store.fired_in_events(text):
        if once(state, text, "fire_blocked_events"):
            store.event(now, "fire blocked", decision="skipped",
                        reason="events.csv already shows a fire today")
        return
    if not fire_mod.valid_url(url) or not fire_mod.valid_token(token):
        rec = state["fire"].setdefault(text, {"attempts": 0})
        rec.update(ok=False, no_retry_today=True, result="config error",
                   error_type="bad url" if not fire_mod.valid_url(url) else "bad token")
        state["fire_config_error"] = True
        store.event(now, "fire attempt", decision="refused", reason=rec["error_type"],
                    detail="nothing was sent")
        return
    persist_cmd = (env.get("STATE_PERSIST_CMD") or "").strip()
    if not persist_cmd and (env.get("GITHUB_ACTIONS") or "").lower() == "true":
        if once(state, text, "fire_blocked_no_persist"):
            store.event(now, "fire blocked", decision="skipped",
                        reason="STATE_PERSIST_CMD is not set in the workflow")
        return
    readings = {}
    for ticker in cfg["tickers"]:
        view = views.get(ticker)
        try:
            if view is not None and view.fresh:
                ibs = rules.s2_crosscheck(view.today, view.price()[0])["IBS"]
                if ibs is not None:
                    readings[ticker] = fmt(ibs, 2)
        except Exception:
            pass  # readings are optional; a fault here must not block the start
    message = fire_mod.build_text(fcfg["marker"], now, readings, cfg["tickers"])
    rec = state["fire"].setdefault(text, {"attempts": 0})
    posts = 0
    while True:
        # Write the claim to disk BEFORE the request goes out. If this run
        # dies mid-call, the next run sees the open claim and does not fire.
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["pending"] = True
        rec["last_attempt"] = now.strftime("%H:%M:%S")
        store.commit()
        if posts == 0 and persist_cmd and not persist(persist_cmd, store.root):
            rec["attempts"] -= 1
            rec["pending"] = False
            store.event(now, "fire blocked", decision="skipped",
                        reason="the claim could not be saved to the repo first",
                        detail="nothing was sent")
            store.commit()
            return
        attempt = sender(url, token, message)
        posts += 1
        rec["pending"] = False
        rec["http_status"] = attempt.http_status
        rec["error_type"] = attempt.error_type
        if attempt.outcome == fire_mod.OK:
            rec.update(ok=True, result="ok", session_id=attempt.session_id)
            state["fire_config_error"] = False
        elif attempt.outcome == fire_mod.UNCERTAIN:
            rec.update(ok=True, result="uncertain")
        elif attempt.outcome in (fire_mod.CONFIG_ERROR, fire_mod.REFUSED):
            rec.update(ok=False, no_retry_today=True, result="config error")
            state["fire_config_error"] = True
        else:
            rec.update(ok=False, result="failed")
        store.event(now, "fire attempt", decision=attempt.outcome,
                    reason=attempt.error_type,
                    detail="attempt=%d http=%s session=%s" %
                    (rec["attempts"], attempt.http_status if attempt.http_status else "none",
                     attempt.session_id or "none"))
        store.commit()
        if attempt.outcome != fire_mod.RETRYABLE:
            return
        if posts >= fire_mod.MAX_POSTS_PER_RUN or rec["attempts"] >= max_attempts:
            return
        sleep(fire_mod.RETRY_DELAYS[posts - 1])


# -------------------------------------------------------------------- main

def parse_now(text, tz):
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m scanner.scan",
                                     description="SENTINEL cloud scanner (paper only).")
    parser.add_argument("--dry-run", action="store_true",
                        help="no fire, no state write, prints decisions")
    parser.add_argument("--now", help='override the clock, e.g. "2026-11-24T15:41:00-05:00"')
    parser.add_argument("--allow-fire", action="store_true",
                        help="with --now, allow the real fire call (off by default)")
    parser.add_argument("--root", help="repo root that holds state/ and HALT (default: repo)")
    parser.add_argument("--fixture-dir",
                        help="read Yahoo-shaped TICKER.json files from this folder, no network")
    return parser


def main(argv=None, *, env=None, root=None, cfg=None, fetcher=None, sender=None,
         persist=None, sleep=_time.sleep, clock=None, out=None):
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    out = out or sys.stdout
    cfg = cfg or load_config()
    tz = ZoneInfo(cfg["timezone"])
    root = root or args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.now:
        now = parse_now(args.now, tz)
    else:
        now = (clock() if clock else datetime.now(tz)).astimezone(tz)
    now = now.replace(microsecond=0)
    today = now.date()
    text = today.isoformat()
    store = Store(root, args.dry_run, out)
    store.say("SCAN now=%s dry_run=%s" % (now.isoformat(), args.dry_run))
    if not store.lock():
        store.say("another scan holds the lock; nothing done")
        return 0
    try:
        try:
            state = store.load(now)
        except StateError as exc:
            # Without readable state the scanner cannot know if it fired today.
            store.say("STATE ERROR: %s. Nothing done, nothing fired." % exc)
            return 1

        if os.path.exists(os.path.join(root, "HALT")):
            store.heartbeat(now, "HALT", "HALT file present; nothing done")
            return 0

        resolve_pending_fires(store, now)
        prune(state, today)

        def finish(status, note, code=0):
            store.commit()
            if "fire_missed" in state["days"].get(text, {}).get("notes", []):
                status += "; NO FIRE TODAY"
            if state.get("fire_config_error"):
                status += "; FIRE CONFIG ERROR"
            store.heartbeat(now, status, note)
            return code

        if today > rules.as_date(cfg["calendar_end"]):
            if once(state, text, "calendar_ended"):
                store.event(now, "calendar ended", decision="skipped",
                            reason="config calendar_end has passed; update the holiday list")
            return finish("calendar ended", "nothing done")
        if not rules.is_trading_day(today, cfg):
            return finish("market closed", "not a trading day")
        if rules.is_half_day(today, cfg):
            if once(state, text, "half_day"):
                store.event(now, "half day, no action", decision="skipped", reason="half day")
            return finish("half day", "no entries, no exit checks, no fire")
        if not (rules.at(today, SCAN_START) <= now <= rules.at(today, SCAN_END)):
            return finish("outside hours", "active from 09:30 to 16:05 ET")

        # Step 3: data
        if fetcher is None:
            if args.fixture_dir:
                fetcher = lambda t, c, e, n: data_mod.load_fixture_dir(args.fixture_dir, t, c)
            else:
                fetcher = data_mod.fetch_all
        try:
            parsed, errors = fetcher(cfg["tickers"], cfg, env, now)
        except Exception as exc:
            parsed, errors = {}, {t: type(exc).__name__ for t in cfg["tickers"]}
        # Live runs: the last trade can be stamped after `now` was read, because
        # the fetch happens later. Measure its age at the time the fetch ended.
        # Replays (--now) keep `now`, so a price from the future is never used.
        price_asof = now
        if not args.now:
            fetched = (clock() if clock else datetime.now(tz)).astimezone(tz)
            price_asof = max(now, fetched.replace(microsecond=0) + timedelta(seconds=1))
        views = {}
        for ticker in cfg["tickers"]:
            if ticker in parsed:
                try:
                    views[ticker] = View(parsed[ticker], now, today, cfg, price_asof)
                except Exception as exc:
                    errors[ticker] = "bad data: " + type(exc).__name__
            elif ticker not in errors:
                errors[ticker] = "no data returned"
        for ticker in cfg["tickers"]:
            if ticker in errors and once(state, text, "data_error:" + ticker):
                store.event(now, "error", ticker, decision="skipped",
                            reason="data fetch failed", detail=str(errors[ticker])[:200])

        # Steps 4 and 5: paper engine
        engine_failed = False
        try:
            log_missed(store, cfg, now, today)
            run_checkpoints(store, cfg, now, today, views)
            run_exit_window(store, cfg, now, today, views)
        except Exception as exc:
            # A fault in the paper engine must not block the backup start.
            engine_failed = True
            if once(state, text, "engine_error:" + type(exc).__name__):
                store.event(now, "error", decision="skipped", reason="paper engine failed",
                            detail=type(exc).__name__)
        store.commit()

        # Step 6: fire. It does not depend on the data fetch.
        run_fire(store, cfg, now, today, views, env, args,
                 sender or fire_mod.send_once, persist or default_persist, sleep)

        fire_rec = state["fire"].get(text, {})
        fire_ready = bool(cfg["fire"].get("enabled")) and not store.dry \
            and not (args.now and not args.allow_fire) \
            and bool((env.get("ROUTINE_FIRE_URL") or "").strip()) \
            and bool((env.get("ROUTINE_FIRE_TOKEN") or "").strip())
        if fire_ready and now > rules.at(today, cfg["fire"]["window"][1]) \
                and not fire_rec.get("ok") and not fire_rec.get("no_retry_today") \
                and once(state, text, "fire_missed"):
            store.event(now, "FIRE MISSED", decision="skipped",
                        reason="the fire window passed with no start sent",
                        detail="attempts=%s last=%s" % (fire_rec.get("attempts", 0),
                                                        fire_rec.get("result", "none")))
        if engine_failed:
            return finish("engine error", "paper engine failed; see events.csv", 1)
        all_failed = not views
        if all_failed:
            return finish("data error", "no ticker could be read", 1)
        if errors:
            return finish("partial data", "failed: " + "/".join(sorted(errors)))
        return finish("ok", "scan complete")
    finally:
        store.unlock()


if __name__ == "__main__":
    sys.exit(main())
