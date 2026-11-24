# SENTINEL cloud scanner

A small Python program that runs on GitHub Actions every 5 minutes during US
market hours. Nothing runs on your PC. Standard library only.

It does three jobs:

1. **Paper engine for rule S2-E.** Paper only. It never places an order and
   holds no broker credentials.
2. **Backup start of the real-money order task.** Once per trading day,
   between 15:25:00 and 15:42:59 ET, it sends one HTTP POST to a Claude
   routine "fire" endpoint. The routine does all broker reads, limit checks
   and orders. The scanner only starts it.
3. **Cross-check log.** At 15:40 ET it records its own reading of the plain
   S2 signal (IBS per ticker) so you can compare it with the live task's log.

The frozen rule text lives in the SENTINEL project doc `claude/108a_PREREG_S2E.md`.
Section 2 of that doc ("Paper" lines) is what this program applies.

## Files

| Path | What it is |
|---|---|
| `scanner/config.json` | All parameters: tickers, holidays, half days, rule numbers, fire window |
| `scanner/rules.py` | The trading rules. Pure functions, no I/O, no clock reads |
| `scanner/data.py` | Price data fetch (Yahoo, optional Alpaca fallback) and parsing |
| `scanner/fire.py` | The routine fire call |
| `scanner/scan.py` | Entry point |
| `scripts/commit_state.sh` | Commits and pushes `state/` (never force pushes) |
| `state/state.json` | Machine state: open paper positions, what was done today, fire record |
| `state/paper_trades.csv` | One row per closed paper trade |
| `state/events.csv` | Log: checkpoint evaluations, entries, exits, fire attempts, S2 cross-check, errors |
| `state/heartbeat.json` | Last scan time and status |
| `HALT.example` | How to stop the scanner |
| `tests/` | Offline unit tests and fixtures |

The `state/` files are created by the first run.

## Setup

1. Push this folder to a private GitHub repo. The default branch must be
   named `main` (the scan job refuses to run from any other branch).
2. In the repo: Settings, Secrets and variables, Actions. Add:
   - `ROUTINE_FIRE_URL` : the full fire URL of the routine. It must start
     with `https://api.anthropic.com/v1/claude_code/routines/` and end with
     `/fire`, or the scanner refuses to send.
   - `ROUTINE_FIRE_TOKEN` : the routine token.
   - Optional: `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY` for the data fallback.
3. Without the two fire secrets the scanner runs in paper-only mode and logs
   "fire not configured" once a day.

The workflow sets `contents: write` itself, so the job can commit the
`state/` folder with no extra repo setting.

## How to stop it

Copy `HALT.example` to a file named `HALT` in the repo root and commit it.
While that file exists the scanner does nothing except write the heartbeat:
no paper trades, no exit checks, no fire. Delete the file to start again.

## How to read the status

Open `state/heartbeat.json`. Status values:

- `ok` : last scan worked.
- `partial data` or `data error` : some or all tickers could not be read.
- `engine error` : the paper engine hit a fault (see the `error` rows in
  `state/events.csv`). The fire step still runs.
- `market closed`, `half day`, `outside hours`, `HALT`, `calendar ended`.
- `FIRE CONFIG ERROR` (added to the status): the fire call was rejected
  (HTTP 400, 401, 403 or 404) or the URL or token is malformed. Fix the secrets.
- `NO FIRE TODAY` (added to the status): the fire window ended and no start
  was sent or recorded today. `state/events.csv` has one "FIRE MISSED" row.

The heartbeat file is rewritten at most once an hour unless something in it
changes, to keep the number of commits low.

## The fire call: one start per day, at most

The fire endpoint has no protection against repeats. Every accepted request
starts a new session of the order task. So the scanner follows these rules:

- At most ONE request that may have reached the server per trading day.
- HTTP 200 with type `routine_fire`: done for the day.
- HTTP 500 or 503, a timeout, or a dropped connection after the request was
  sent: recorded as "uncertain", counted as done for the day, never sent
  again. A missed backup start is better than a double start.
- A failure before anything was sent (DNS failure, connection refused, TLS
  failure): up to 2 more tries in the same run (3 and 8 seconds apart) and
  later scans may try, up to `max_attempts_per_day` connection tries in total.
- HTTP 429: no retry in the same run. A later scan may try.
- HTTP 400, 401, 403, 404: config error, no retry that day.
- Before the request goes out, the scanner writes a claim into `state.json`
  and, on GitHub, commits and pushes it (`STATE_PERSIST_CMD` in the
  workflow). If that push fails, nothing is sent. If a run dies in the middle
  of the call, the next run finds the open claim and does not fire.
- No fire on half days, holidays, weekends, with `HALT` present, or after
  `calendar_end`.
- The token and the fire URL are never written to any log, event, state file
  or output.

Do not run `python -m scanner.scan` on your own PC with the fire secrets set
while the GitHub schedule is also running. Two copies with separate state
folders could each send a start.

## Local runs

Tests (offline, no network):

    python -m unittest discover -s tests -v

Dry run for a given clock time, using live price data. Nothing is written
and nothing is sent:

    python -m scanner.scan --dry-run --now "2026-11-24T15:41:00-05:00"

Dry run with no network, using the test fixture bars (Nov 20, 23 and 24, 2026):

    python -m scanner.scan --dry-run --now "2026-11-23T11:00:00-05:00" --fixture-dir tests/fixtures/replay

Notes on `--now`:

- A time with no offset is read as New York time.
- With `--now` the fire call is forced off unless `--allow-fire` is also given.
- `--dry-run` never fires and never writes, with or without `--now`.
- `--root PATH` points the scanner at another folder for `state/` and `HALT`.

## Yearly upkeep

`holidays`, `half_days` and `calendar_end` in `scanner/config.json` cover
dates through 2027-12-31. After that date the scanner logs "calendar ended"
once a day and does nothing else until the lists are extended.

## Rule summary (see the frozen doc for the exact text)

- Tickers SPY, QQQ, IWM, DIA. $100 paper notional per trade. One open paper
  position per ticker, one entry per ticker per day.
- Checkpoints 11:00, 12:00, 13:00, 14:00, 15:00 ET. The first scan from the
  checkpoint to 9 minutes 59 seconds after it decides. H and L come from
  finished 5-minute bars that start at or after 09:30 and before the
  checkpoint. P is the close of the bar that starts 5 minutes before it.
  Entry if (P - L) / (H - L) is below 0.20 and (H - L) / L is at least 0.005.
- If a scan inside a checkpoint or exit window has stale data or a failed
  fetch for a ticker, the next scan inside the same window may try again.
- Paper entry price: the last trade price if it is under 180 seconds old,
  else the close of the newest finished bar (flagged in the log). A last
  price more than 5% away from the newest finished bar close is not used.
- With the Alpaca fallback the "last price" is the close of a bar, not a trade.
- Exit check: first scan from 15:40:00 to 15:49:59 ET, never on the entry
  day. Sell if the price is above the previous trading day's high, or IBS is
  above 0.50, or it is the 5th or a later trading day after entry.
- The previous day's high is used only if that day has at least 75 of its 78
  bars (40 of 42 on a half day). If not, only the IBS and time exits apply.
- Half days: no entries, no exit checks, no fire. They still count as
  trading days for the hold count.
- Cost: 5 basis points per round trip, charged at exit.
