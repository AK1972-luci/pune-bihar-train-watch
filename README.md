# Pune → Bihar Special Train Watcher

Watches for **any newly announced / newly listed train** (specials first, but every new train on
the route is reported) from **Pune (PUNE / Hadapsar HDP)** towards **Patna (PNBE), Muzaffarpur (MFP),
Danapur (DNR)** and the nearby stations **Patliputra (PPTA), Hajipur (HJP), Rajendranagar (RJPB)**,
and pushes an **instant phone notification**. Runs from now until **5 Nov 2026**.

Priority: any departure **20 Oct – 5 Nov 2026** → `urgent`. 18, 19 Oct and 6, 7 Nov → `urgent (nearby date)`.
Any other new train on the route → `high` (and it keeps watching for the schedule to be extended
into your dates, which triggers another `urgent` alert).

## What it polls

| Source | Every | What it detects |
|---|---|---|
| erail.in train list (IR data) | 3 min | A new train number / schedule appearing between Pune and Bihar, or an existing train's validity being extended to cover your dates. This is the "schedule is live, go book" signal. |
| Central Railway press releases | 5 min | Zonal announcements ("CR announces N Diwali/Chhath specials, bookings open on …"). Body text is parsed for train numbers, dates, booking-opening lines. |
| PIB (Govt of India) RSS | 5 min | Railway Ministry announcements. |
| Google News RSS, English + Hindi (11 queries) | 5 min | Media coverage of CR / East Central Railway announcements. Only articles published after the watcher started. |
| RapidAPI IRCTC availability (optional) | 3 min | Seat availability for each special on your dates; sends **BOOKING OPEN** the moment a class becomes bookable. Needs a free RapidAPI key, see below. |

Everything seen on the very first run is recorded as a baseline and **never** alerted, so you only
hear about things that are new.

## 1. Get the notifications on your phone (do this now)

The watcher is already running and publishing to a private ntfy topic:

```
topic:  <see config.local.json on the laptop>
```

1. Install **ntfy** (Android: Play Store / F-Droid, iPhone: App Store).
2. Tap **+ Subscribe to topic**, enter the topic from `config.local.json`.
3. In the app, set the topic's notification priority so `urgent` alerts override Do-Not-Disturb.
4. Send yourself a test:

```bash
cd ~/train && python3 special_train_watch.py --test-notify
```

The topic name is a secret: on the laptop it lives in the untracked `config.local.json`, in the
cloud in the repo secret `NTFY_TOPIC`. It is deliberately absent from `config.json`, README and
git history because the repo is public. To change it, update both places and restart.

### Optional: Telegram as well

Create a bot with @BotFather, send it any message, get your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`, then fill `telegram_bot_token` and
`telegram_chat_id` in `config.json` and restart.

### Optional: live seat availability + "BOOKING OPEN" alerts

IRCTC has no public API. The watcher supports the **IRCTC1** API on RapidAPI
(`irctc1.p.rapidapi.com`, free tier is enough): subscribe, copy your key into
`availability.rapidapi_key` in `config.json`, restart. Without it, you still get the instant
"schedule appeared" alert and then check IRCTC yourself.

## 2. Fill in your journey details

Edit `config.json`:

```json
"passengers": 1,
"class_preference": "ANY",   // SL / 3A / 2A / 1A / ANY
"quota": "GN"
```

Then restart:

```bash
systemctl --user restart train-watch.service
```

## 3. Running / checking

Installed as a systemd user service (`~/.config/systemd/user/train-watch.service`), auto-restarts,
lingering enabled so it does not need an open terminal.

```bash
systemctl --user status train-watch.service     # is it alive?
tail -f ~/train/watch.log                       # what it is doing
cat ~/train/alerts.log                          # every alert ever sent
systemctl --user restart train-watch.service    # after editing config.json
systemctl --user stop train-watch.service       # stop
```

A daily **09:00 IST heartbeat** ("watcher alive") confirms it is still running. If you don't get
it one morning, the laptop/WSL was off.

### Keep it alive: WSL and the laptop must be running

The service dies when WSL shuts down (Windows stops the WSL VM a few seconds after the last
WSL window closes) or when the laptop sleeps. Options, from simplest:

Already configured on 13 Sep 2026:

* `%UserProfile%\.wslconfig` has `vmIdleTimeout=-1`, so the WSL VM no longer stops when the last
  terminal closes (backup of the old file: `.wslconfig.bak-2026-09-13`).
* A hidden Startup script `TrainWatchWSL.vbs` (Start Menu → Programs → Startup) boots WSL at every
  Windows logon, which starts the service via systemd.

Still on you: the laptop must stay powered on and **not sleep** (Windows Settings → System → Power
→ Screen and sleep → "Never" when plugged in). Sleep or shutdown pauses the watcher; it catches up
on restart but anything announced meanwhile is only seen then.

### Run it in the cloud instead (works with the laptop off)

Set up on 14 Sep 2026: public repo **https://github.com/AK1972-luci/pune-bihar-train-watch**.
GitHub's cron never fired on this repo, so the workflow `.github/workflows/watch.yml` is a
self-chaining job: each run polls continuously for ~5.5 h, queues its successor at start, and commits
`state.json` / `alerts.log` back every 20 min. Public repo = unlimited free Actions minutes.
If the chain ever stops (no "(github)" heartbeat one morning), restart it with
`gh workflow run watch.yml` or the "Run workflow" button in the Actions tab.
Secret `NTFY_TOPIC` is set; add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `RAPIDAPI_KEY` under
repo Settings → Secrets if you enable those. Its daily heartbeat is labelled "(github)", the
laptop's "(laptop)". Both run independently, so a real alert may arrive twice. Check runs at
Actions tab, or `gh run list --workflow=watch.yml`. To push config changes:
`cd ~/train && git add -A && git commit -m "update" && git pull --rebase && git push`.

## Alert contents

Each train alert carries: train name and number, special/regular type and zone, boarding station
and departure time, alighting station and arrival time, the Pune departure dates that match your
window (target dates flagged), run days and validity window, classes and fare tiers, availability
(if the RapidAPI key is set), your passenger/class/quota settings, IRCTC and erail links, source,
and the exact detection time (IST). Announcement alerts carry the headline, link, special train
numbers and dates parsed from the text, any "booking opens …" sentence, and an excerpt around the
Pune / Patna / Danapur / Muzaffarpur mention.

## Files

* `special_train_watch.py` — the watcher (`--once`, `--dry-run`, `--test-notify`, `--reset`, `-v`)
* `config.json` — your settings and notification channels
* `state.json` — what has already been seen (delete or `--reset` to re-baseline)
* `alerts.log`, `watch.log` — history
* `github-actions-watch.yml` — optional cloud runner
