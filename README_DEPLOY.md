# Deploy: ongoing World Cup closing-line collector

This turns the `closingline` folder into an **always-on** job that captures
Pinnacle's **closing line** for every 2026 FIFA World Cup match and writes it to a
**Google Sheet** (with JSON + CSV backups committed into the repo). It runs on a
schedule in **GitHub Actions**, so it keeps working even when your laptop is off.

Three markets are captured per match:

| Market | The Odds API key | What it is |
|--------|------------------|------------|
| 1X2 | `h2h` | Home / Draw / Away |
| Asian Handicap | `spreads` | Pinnacle soccer spreads are Asian handicaps |
| Asian Total Goals | `totals` | Pinnacle soccer totals are Asian totals |

Each market is de-vigged to a fair probability (Shin method, best for a sharp book
like Pinnacle; multiplicative kept alongside in the JSON).

---

## What's in the project

| file | role |
|------|------|
| `wc_collector.py` | **The collector.** Lists WC fixtures, pulls each match's closing line once it has kicked off, de-vigs, writes rows. |
| `sheets.py` | Google Sheets writer (append-only, de-duplicated by `match_id`). |
| `requirements.txt` | Python deps — only needed for the Google Sheet output. |
| `.github/workflows/collect-closing-lines.yml` | The scheduled GitHub Action (every 3 h). |
| `test_wc_offline.py` | Offline tests (no network, no credits). |
| `data/` | Output: `world_cup_closing_lines.json` / `.csv` + `captured_state.json`. Created on first run. |
| `.env.example` | Template for local (VS Code) runs. |
| `closing_line.py`, `sample_input.csv`, `test_offline.py`, `README.md` | Your original bet-CLV tool — untouched. |

**Idempotent by design:** a match is captured exactly once. Re-running never
double-charges API credits or duplicates rows. State lives in
`data/captured_state.json` (and the Sheet is also de-duped by `match_id`).

**Cost:** ~30 credits per match (3 markets × 10). ~104 matches ≈ **~3,100 credits**
for the whole tournament. Listing fixtures is free.

---

## Part A — Try it locally first (VS Code)

Do this once to confirm your API key works and see real output before scheduling.

```bash
cd closingline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # only needed if you want the Sheet
cp .env.example .env                      # then edit .env, put your ODDS_API_KEY in

# Load the .env into your shell, then dry-run (spends NOTHING — just shows matches):
export $(grep -v '^#' .env | xargs)
python3 wc_collector.py --dry-run
```

If the dry-run lists World Cup matches, you're wired up. To actually capture the
matches that have already kicked off (this spends credits):

```bash
python3 wc_collector.py --backfill        # every past match not yet captured
# or just: python3 wc_collector.py        # normal incremental run
```

Output lands in `data/`. Open `data/world_cup_closing_lines.csv` to eyeball it.
(You can stop here and just run it manually if you prefer — but Part B makes it
truly hands-off.)

Run the tests any time — no network, no credits:

```bash
python3 test_offline.py && python3 test_wc_offline.py
```

---

## Part B — Put it on GitHub Actions (the "ongoing" part)

### B1. Create the repo

From inside the `closingline` folder:

```bash
git init
git add .
git commit -m "World Cup closing-line collector"
```

Then create a **private** GitHub repo and push. Easiest with the GitHub CLI:

```bash
gh repo create closingline --private --source . --push
```

(No `gh`? Create an empty private repo on github.com, then run the
`git remote add origin …` / `git push -u origin main` commands GitHub shows you.)

> `.gitignore` already excludes `.env` and any service-account JSON, so you can't
> accidentally commit a secret.

### B2. Add the repository Secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret.**
Add:

| Secret name | Value |
|-------------|-------|
| `ODDS_API_KEY` | Your The Odds API key |
| `WC_SHEET_ID` | Your Google Sheet id (see B3). *Omit to skip the Sheet and keep JSON/CSV only.* |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire** service-account JSON (see B3). *Omit if not using the Sheet.* |

(Optional **Variables**, not secrets, if you ever want to change defaults:
`WC_SHEET_TAB`, `WC_MARKETS`, `WC_SPORT_KEY`.)

### B3. Google Sheet setup (skip if you only want JSON/CSV)

1. Go to <https://console.cloud.google.com/> → create a project (any name).
2. **APIs & Services → Library →** enable **Google Sheets API**.
3. **APIs & Services → Credentials → Create credentials → Service account.** Give
   it a name, create it.
4. Open the service account → **Keys → Add key → Create new key → JSON.** A `.json`
   file downloads. Its contents go into the `GOOGLE_SERVICE_ACCOUNT_JSON` secret.
5. Copy the service account's email — it looks like
   `something@your-project.iam.gserviceaccount.com`.
6. Create a Google Sheet. **Share it with that service-account email as an Editor.**
7. The sheet id is the long string in its URL between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`THIS_IS_THE_ID`**`/edit`. Put it in
   the `WC_SHEET_ID` secret.

The collector creates a `closing_lines` tab and header row automatically on first
write.

### B4. First run + confirm

In the repo: **Actions → "Collect World Cup closing lines" → Run workflow.** Tick
**backfill** for the first run to grab every match played so far. Watch the log:
it prints each match captured and the API quota remaining. When it finishes, check
your Google Sheet (rows appended) and the repo's `data/` folder (JSON/CSV
committed).

After that, it runs **every 3 hours automatically** and picks up each new match's
close. Nothing else to do.

---

## Tuning

- **Cadence:** edit the `cron` in `.github/workflows/collect-closing-lines.yml`.
  `0 */3 * * *` = every 3 h (UTC). Every 6 h is fine too and halves Action minutes.
- **Only recent matches:** set a `WC_LOOKBACK_HOURS` variable (e.g. `12`) so a run
  only looks at matches kicked off in the last N hours. Default `0` = no limit.
- **Fewer markets / cheaper:** set a `WC_MARKETS` variable to e.g. `h2h` to track
  1X2 only (~10 credits/match).
- **After the tournament:** the same setup works for any competition — change the
  `WC_SPORT_KEY` variable (e.g. `soccer_epl`) and it collects that league's closes.

---

## How the closing line is defined

The Odds API historical endpoint returns the snapshot **≤** the timestamp you ask
for. The collector asks at each match's true kickoff time, so it gets the last
pre-kickoff Pinnacle snapshot = the closing line (snapshots are ~5 min apart, so
the close is at most ~5 min stale). A match is only queried once `now ≥ kickoff`.

This tool reads odds data only. It never logs into, places, or settles bets anywhere.
