#!/usr/bin/env python3
"""
Special Train Tracking & Instant Notification System
Pune -> Patna / Muzaffarpur / Danapur, 3-5 November 2026

Watches four kinds of sources and pushes an instant phone notification
(ntfy.sh and/or Telegram) the moment something NEW and relevant appears:

  1. erail.in train list   - the authoritative "a new train number / schedule now
                             exists between Pune and Bihar" signal. Special trains
                             (0xxxx numbers) show up here with their valid-from /
                             valid-to dates as soon as the schedule is published.
  2. Central Railway press releases (cr.indianrailways.gov.in) - zonal
                             announcements ("CR announces N Diwali/Chhath specials,
                             bookings open on ...").
  3. PIB (Press Information Bureau) RSS - Railway Ministry announcements.
  4. Google News RSS (English + Hindi) - media coverage of CR / ECR announcements.

Optional: seat availability via a RapidAPI IRCTC key (see config.json).

State is kept in state.json so only NEW items ever notify.  Run continuously
(default) or once (--once) from cron / GitHub Actions.
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "origins": ["PUNE", "HDP"],                # Pune Jn, Hadapsar (Pune)
    "destinations": ["PNBE", "MFP", "DNR"],    # Patna Jn, Muzaffarpur Jn, Danapur
    "nearby_destinations": ["PPTA", "HJP", "RJPB"],  # Patliputra, Hajipur, Rajendranagar
    "target_dates": ["2026-11-03", "2026-11-04", "2026-11-05"],
    "nearby_dates": ["2026-11-01", "2026-11-02", "2026-11-06", "2026-11-07"],
    "passengers": 1,
    "class_preference": "ANY",                 # SL / 3A / 2A / 1A / ANY
    "quota": "GN",
    "stop_after": "2026-11-05",
    "poll_seconds": 180,                       # main loop interval
    "press_poll_seconds": 300,                 # CR / PIB / news interval
    "daily_heartbeat_ist": "09:00",            # "" to disable
    "notify": {
        "ntfy_topic": "",                      # e.g. "pune-bihar-special-ak7731"
        "ntfy_server": "https://ntfy.sh",
        "telegram_bot_token": "",
        "telegram_chat_id": ""
    },
    "availability": {
        "rapidapi_key": "",                    # RapidAPI "IRCTC1" key (optional)
        "rapidapi_host": "irctc1.p.rapidapi.com",
        "classes_to_check": ["SL", "3A", "2A"]
    },
    "news_queries": [
        "Pune special train Patna",
        "Pune special train Danapur",
        "Pune special train Muzaffarpur",
        "Pune Bihar special train Diwali Chhath",
        "Central Railway special trains Diwali Chhath 2026",
        "Central Railway Pune division special trains November",
        "East Central Railway special train Pune",
        "पुणे स्पेशल ट्रेन पटना",
        "पुणे दानापुर स्पेशल ट्रेन",
        "पुणे मुजफ्फरपुर स्पेशल ट्रेन",
        "दिवाली छठ स्पेशल ट्रेन पुणे बिहार"
    ]
}

STATION_NAMES = {
    "PUNE": "Pune Jn", "HDP": "Hadapsar (Pune)", "PNBE": "Patna Jn",
    "MFP": "Muzaffarpur Jn", "DNR": "Danapur", "PPTA": "Patliputra",
    "HJP": "Hajipur Jn", "RJPB": "Rajendranagar (Patna)",
}

# keyword sets used to judge relevance of a press release / article
KW_ORIGIN = [r"\bpune\b", r"hadapsar", r"पुणे", r"हडपसर"]
KW_DEST = [r"\bpatna\b", r"danapur", r"muzaffarpur", r"patliputra", r"hajipur",
           r"rajendra ?nagar", r"\bbihar\b", r"पटना", r"दानापुर", r"मुजफ्फरपुर",
           r"पाटलिपुत्र", r"हाजीपुर", r"बिहार"]
KW_SPECIAL = [r"special", r"festival", r"diwali", r"deepavali", r"chhath",
              r"additional", r"extra", r"holiday", r"puja", r"स्पेशल", r"विशेष",
              r"दिवाली", r"दीपावली", r"छठ", r"अतिरिक्त", r"त्योहार"]
KW_RAIL = [r"rail", r"train", r"irctc", r"रेल", r"ट्रेन"]
KW_BOOKING = [r"booking[s]? (?:will )?(?:open|start|commence)[^.]{0,80}",
              r"reservation[s]? (?:will )?(?:open|start|commence)[^.]{0,80}",
              r"बुकिंग[^।.]{0,80}", r"आरक्षण[^।.]{0,80}"]

TRAIN_NO_RE = re.compile(r"(?<!\d)(0\d{4})(?!\d)")      # special-train numbers 0xxxx
DATE_RE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](20\d{2})")

log = logging.getLogger("watch")


def now_ist():
    return dt.datetime.now(IST)


def stamp():
    return now_ist().strftime("%d-%b-%Y %H:%M:%S IST")


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for cand in (path, path.with_name("config.local.json")):   # config.local.json: untracked, holds secrets
        if not cand.exists():
            continue
        user = json.loads(cand.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    # environment overrides for secrets (handy for GitHub Actions)
    env = os.environ
    cfg["notify"]["ntfy_topic"] = env.get("NTFY_TOPIC", cfg["notify"]["ntfy_topic"])
    cfg["notify"]["telegram_bot_token"] = env.get("TELEGRAM_BOT_TOKEN", cfg["notify"]["telegram_bot_token"])
    cfg["notify"]["telegram_chat_id"] = env.get("TELEGRAM_CHAT_ID", cfg["notify"]["telegram_chat_id"])
    cfg["availability"]["rapidapi_key"] = env.get("RAPIDAPI_KEY", cfg["availability"]["rapidapi_key"])
    return cfg


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, path):
        self.path = path
        self.data = {"started": stamp(), "erail": {}, "press": {}, "news": {},
                     "pib": {}, "avail": {}, "last_press_poll": 0, "last_heartbeat": ""}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                log.warning("state file unreadable, starting fresh")

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def first_run(self):
        return not self.data["erail"] and not self.data["press"] and not self.data["news"]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
session = requests.Session()
session.headers["User-Agent"] = UA


def get(url, timeout=25, retries=1, **kw):
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt >= retries:
                raise
            time.sleep(3)


def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
class Notifier:
    def __init__(self, cfg, dry=False):
        self.cfg = cfg["notify"]
        self.dry = dry
        self.alerts_file = HERE / "alerts.log"

    def send(self, title, body, priority="high", tags=None):
        """priority: urgent | high | default | low"""
        line = f"\n===== {stamp()} [{priority}] {title} =====\n{body}\n"
        with self.alerts_file.open("a", encoding="utf-8") as f:
            f.write(line)
        log.info("ALERT %s: %s", priority.upper(), title)
        if self.dry:
            print(line)
            return
        ok = False
        ok |= self._ntfy(title, body, priority, tags or [])
        ok |= self._telegram(title, body)
        if not ok:
            log.warning("No notification channel configured or all failed; alert only logged.")

    def _ntfy(self, title, body, priority, tags):
        topic = self.cfg.get("ntfy_topic")
        if not topic:
            return False
        prio = {"urgent": "5", "high": "4", "default": "3", "low": "2"}.get(priority, "3")
        try:
            r = session.post(f"{self.cfg['ntfy_server'].rstrip('/')}/{topic}",
                             data=body.encode("utf-8"),
                             headers={"Title": title.encode("utf-8"), "Priority": prio,
                                      "Tags": ",".join(["train2"] + tags)},
                             timeout=20)
            return r.ok
        except Exception as e:
            log.error("ntfy failed: %s", e)
            return False

    def _telegram(self, title, body):
        tok, chat = self.cfg.get("telegram_bot_token"), self.cfg.get("telegram_chat_id")
        if not (tok and chat):
            return False
        text = f"*{title}*\n{body}"
        try:
            r = session.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                             json={"chat_id": chat, "text": text[:4000],
                                   "disable_web_page_preview": True}, timeout=20)
            if not r.ok:   # markdown may break on odd chars; retry plain
                r = session.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                 json={"chat_id": chat, "text": text[:4000]}, timeout=20)
            return r.ok
        except Exception as e:
            log.error("telegram failed: %s", e)
            return False


# --------------------------------------------------------------------------- #
# Helpers: dates, relevance
# --------------------------------------------------------------------------- #
def parse_date(s):
    try:
        return dt.date.fromisoformat(s)
    except Exception:
        return None


def runs_on(train, date):
    """Does this erail record depart origin on `date`? days = Mon..Sun bitstring."""
    vf, vt = parse_date(train["valid_from"]), parse_date(train["valid_to"])
    if vf and date < vf:
        return False
    if vt and date > vt:
        return False
    days = train["days"]
    return len(days) == 7 and days[date.weekday()] == "1"


def matching_dates(train, dates):
    return [d for d in dates if runs_on(train, dt.date.fromisoformat(d))]


def irctc_link():
    return "https://www.irctc.co.in/nget/train-search"


def fmt_time(t):
    return t.replace(".", ":") if t else "?"


def preferred_classes(cfg):
    pref = cfg.get("class_preference", "ANY")
    if isinstance(pref, str):
        pref = [c.strip() for c in re.split(r"[,/ ]+", pref) if c.strip()]
    pref = [c.upper() for c in pref]
    return [] if not pref or "ANY" in pref else pref


def kw_hits(text, kws):
    t = text.lower()
    return [k for k in kws if re.search(k, t, flags=re.I)]


def relevance(text):
    """Return (score, details) for an announcement text."""
    o, d, s, r = (kw_hits(text, KW_ORIGIN), kw_hits(text, KW_DEST),
                  kw_hits(text, KW_SPECIAL), kw_hits(text, KW_RAIL))
    score = 0
    if r or s:
        score += 1
    if o:
        score += 2
    if d:
        score += 2
    if s:
        score += 1
    return score, {"origin": bool(o), "dest": bool(d), "special": bool(s), "rail": bool(r)}


def extract_facts(text):
    trains = sorted(set(TRAIN_NO_RE.findall(text)))
    dates = []
    for dd, mm, yy in DATE_RE.findall(text):
        try:
            dates.append(dt.date(int(yy), int(mm), int(dd)).isoformat())
        except ValueError:
            pass
    dates = sorted(set(dates))
    booking = []
    for pat in KW_BOOKING:
        booking += [m.group(0).strip() for m in re.finditer(pat, text, flags=re.I)]
    return trains, dates, booking[:3]


def date_priority(dates_found, cfg):
    tgt = set(cfg["target_dates"])
    near = set(cfg["nearby_dates"])
    if any(d in tgt for d in dates_found):
        return "TARGET DATE"
    if any(d in near for d in dates_found):
        return "NEARBY DATE"
    # date ranges "from X to Y" covering target dates
    ds = [parse_date(d) for d in dates_found if parse_date(d)]
    if len(ds) >= 2:
        lo, hi = min(ds), max(ds)
        if any(lo <= dt.date.fromisoformat(t) <= hi for t in tgt):
            return "TARGET DATE (in announced range)"
    return ""


# --------------------------------------------------------------------------- #
# Source 1: erail.in train list
# --------------------------------------------------------------------------- #
def erail_trains(src, dst):
    url = ("https://erail.in/rail/getTrains.aspx?Station_From={}&Station_To={}"
           "&DataSource=0&Language=0&Cache=false").format(src, dst)
    raw = get(url).text
    out = []
    for rec in raw.split("^")[1:]:
        f = rec.split("~")
        if len(f) < 60 or not f[0].strip():
            continue
        fares = {}
        try:  # field 41: "TYPE:km:1A?..:..." we only keep the raw string for display
            fares_raw = f[41]
        except IndexError:
            fares_raw = ""
        classes = sorted(set(re.findall(r"\b(1A|2A|3A|3E|SL|CC|EC|2S|GN)\b", f[59] if len(f) > 59 else "")))
        out.append({
            "no": f[0], "name": f[1].strip(), "src_name": f[2], "src": f[3],
            "dst_name": f[4], "dst": f[5], "from_name": f[6], "from": f[7],
            "to_name": f[8], "to": f[9], "dep": f[10], "arr": f[11], "dur": f[12],
            "days": f[13], "type": f[32] if len(f) > 32 else "",
            "valid_from": f[37] if len(f) > 37 else "", "valid_to": f[38] if len(f) > 38 else "",
            "classes": classes, "fares_raw": fares_raw,
            "zone": f[53] if len(f) > 53 else "",
        })
    return out


def is_special(t):
    return t["no"].startswith("0") or "SPECIAL" in t["name"].upper() or "SPL" in t["name"].upper()


def fare_summary(t):
    # fares_raw looks like "MAIL_EXPRESS:1748:,,,,,:2725,1340,1570,525,1315,1865:..."
    # groups appear to be per-class blocks; show the non-empty numeric groups compactly
    groups = [g for g in t["fares_raw"].split(":")[2:] if re.search(r"\d", g)]
    return " | ".join(groups[:3]) if groups else "n/a"


def train_alert_body(t, cfg, kind, dates, avail=None, extra=""):
    fmt = lambda d: dt.date.fromisoformat(d).strftime("%a %d %b")
    if len(dates) > 6:
        dep_dates = f"{fmt(dates[0])} … {fmt(dates[-1])} ({len(dates)} departure dates in your window)"
    else:
        dep_dates = ", ".join(fmt(d) for d in dates) or "-"
    tgt = [d for d in dates if d in cfg["target_dates"]]
    lines = [
        f"🚆 {t['name']} ({t['no']})  [{kind}]",
        f"Type: {'SPECIAL' if is_special(t) else 'regular'} {t['type']}  Zone: {t['zone']}",
        f"Route: {t['src_name']} ({t['src']}) → {t['dst_name']} ({t['dst']})",
        f"Boards at: {t['from_name']} ({t['from']}) dep {fmt_time(t['dep'])}",
        f"Alights at: {t['to_name']} ({t['to']}) arr {fmt_time(t['arr'])} (journey {fmt_time(t['dur'])} h)",
        f"Pune departure dates matching you: {dep_dates}" + ("  ← TARGET DATE ✅" if tgt else ""),
        f"Runs on (Mon..Sun): {t['days']}  Valid: {t['valid_from'] or '?'} to {t['valid_to'] or '?'}",
        f"Classes: {', '.join(t['classes']) or 'n/a'}   Fare groups: {fare_summary(t)}",
    ]
    want = preferred_classes(cfg)
    if want:
        have = [c for c in want if c in t["classes"]]
        lines.append(f"Your classes ({'/'.join(want)}): "
                     + (f"{'/'.join(have)} on this train ✅" if have else
                        ("NOT listed on this train ⚠️" if t["classes"] else "coach info not published yet")))
    if avail:
        lines.append("Availability: " + avail)
    if extra:
        lines.append(extra)
    lines += [
        f"Passengers: {cfg['passengers']}  Class pref: {'/'.join(preferred_classes(cfg)) or 'ANY'}  Quota: {cfg['quota']}",
        f"Book: {irctc_link()}",
        f"Check: https://erail.in/trains-between-stations/{t['from']}-to-{t['to']}",
        "Source: erail.in schedule feed (IR data)",
        f"Detected: {stamp()}",
    ]
    return "\n".join(lines)


def check_erail(cfg, state, notifier, avail, baseline=False):
    dests = cfg["destinations"] + cfg["nearby_destinations"]
    seen_now = {}
    for src in cfg["origins"]:
        for dst in cfg["destinations"]:
            try:
                trains = erail_trains(src, dst)
            except Exception as e:
                log.warning("erail %s->%s failed: %s", src, dst, e)
                continue
            for t in trains:
                if t["from"] not in cfg["origins"] or t["to"] not in dests:
                    continue
                key = f"{t['no']}|{t['from']}|{t['to']}"
                seen_now[key] = t
            time.sleep(1 + random.random())

    all_dates = cfg["target_dates"] + cfg["nearby_dates"]
    known = state.data["erail"]
    for key, t in seen_now.items():
        dates = matching_dates(t, all_dates)
        sig = f"{t['valid_from']}|{t['valid_to']}|{t['days']}|{t['dep']}"
        prev = known.get(key)
        special = is_special(t)
        target_hit = any(d in cfg["target_dates"] for d in dates)
        near_hit = bool(dates) and not target_hit

        if prev is None:
            known[key] = {"sig": sig, "first_seen": stamp(), "dates": dates,
                          "booking_open": False, "name": t["name"]}
            if baseline:
                log.info("baseline: %s %s %s->%s valid %s..%s dates=%s", t["no"], t["name"],
                         t["from"], t["to"], t["valid_from"], t["valid_to"], dates)
                continue
            # ANY newly listed train on the Pune -> Bihar route is reported;
            # priority depends on whether it runs on the target / nearby dates.
            label = "SPECIAL" if special else "TRAIN"
            if target_hit:
                notifier.send(f"🚨 NEW {label}: {t['no']} {t['name']} → {t['to']} on your TARGET date",
                              train_alert_body(t, cfg, f"NEW {label} – runs on your dates", dates),
                              "urgent", ["rotating_light"])
            elif near_hit:
                notifier.send(f"🚨 NEW {label}: {t['no']} {t['name']} → {t['to']} (nearby date)",
                              train_alert_body(t, cfg, f"NEW {label} – nearby date", dates),
                              "urgent", ["rotating_light"])
            else:
                notifier.send(f"New {label.lower()} announced: {t['no']} {t['name']} → {t['to']} (not yet on your dates)",
                              train_alert_body(t, cfg, f"NEW {label} ON ROUTE – other dates; will keep watching for extension", dates),
                              "high", ["new"])
        elif prev["sig"] == sig:
            if prev.get("dates") != dates:      # config date window changed; no alert
                prev["dates"] = dates
        else:
            old_dates = set(prev.get("dates", []))
            new_dates = [d for d in dates if d not in old_dates]
            prev.update({"sig": sig, "dates": dates, "changed": stamp()})
            if new_dates:
                kind = "SCHEDULE EXTENDED – now covers your dates"
                notifier.send(f"🚨 {t['no']} {t['name']} now runs on {', '.join(new_dates)}",
                              train_alert_body(t, cfg, kind, dates), "urgent", ["rotating_light"])
            else:
                log.info("schedule change (no new target dates) %s %s", t["no"], t["name"])

        # availability / booking-open tracking for specials on our dates
        if special and dates and avail.enabled:
            check_availability_transitions(t, dates, cfg, state, notifier, avail, known[key])

    # trains that vanished from the list
    for key in list(known):
        if key not in seen_now and not known[key].get("gone"):
            known[key]["gone"] = stamp()
            log.info("train no longer listed: %s", key)
    state.save()


# --------------------------------------------------------------------------- #
# Availability (optional, RapidAPI IRCTC1)
# --------------------------------------------------------------------------- #
class Availability:
    def __init__(self, cfg):
        a = cfg["availability"]
        self.key, self.host = a.get("rapidapi_key"), a.get("rapidapi_host")
        self.classes = a.get("classes_to_check", ["SL", "3A", "2A"])
        self.quota = cfg["quota"]
        self.enabled = bool(self.key)

    def check(self, train_no, src, dst, date_iso, cls):
        url = f"https://{self.host}/api/v1/checkSeatAvailability"
        params = {"classType": cls, "fromStationCode": src, "quota": self.quota,
                  "toStationCode": dst, "trainNo": train_no, "date": date_iso}
        r = session.get(url, params=params, timeout=25,
                        headers={"x-rapidapi-key": self.key, "x-rapidapi-host": self.host})
        j = r.json()
        if not j.get("status"):
            return None
        for row in j.get("data", []):
            d = row.get("date", "")
            parts = re.split(r"[-/]", d)
            try:
                iso = dt.date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
            except Exception:
                continue
            if iso == date_iso:
                return row.get("current_status") or row.get("status") or json.dumps(row)[:80]
        return None


def check_availability_transitions(t, dates, cfg, state, notifier, avail, rec):
    classes = preferred_classes(cfg) or avail.classes
    results = []
    for d in dates:
        for cls in classes:
            k = f"{t['no']}|{t['from']}|{t['to']}|{d}|{cls}"
            try:
                st = avail.check(t["no"], t["from"], t["to"], d, cls)
            except Exception as e:
                log.warning("availability %s failed: %s", k, e)
                continue
            prev = state.data["avail"].get(k)
            state.data["avail"][k] = st
            results.append(f"{d} {cls}: {st or 'not bookable yet'}")
            bookable = st is not None and not re.search(r"NOT AVAILABLE|TRAIN CANCELLED|NOT ALLOWED", st, re.I)
            if bookable and (prev is None or re.search(r"NOT AVAILABLE", str(prev), re.I)):
                notifier.send(f"🎟️ BOOKING OPEN: {t['no']} {t['name']} {d} {cls} → {st}",
                              train_alert_body(t, cfg, "BOOKING OPEN / SEATS AVAILABLE", dates,
                                               avail=f"{d} {cls}: {st}"),
                              "urgent", ["tada"])
                rec["booking_open"] = True
            time.sleep(0.5)
    if results:
        log.info("availability %s: %s", t["no"], "; ".join(results))


# --------------------------------------------------------------------------- #
# Source 2: Central Railway press releases
# --------------------------------------------------------------------------- #
CR_LIST = "https://cr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268"
CR_BASE = "https://cr.indianrailways.gov.in/"


def cr_press_items():
    s = get(CR_LIST, timeout=60, retries=2).text
    items = []
    for href, title in re.findall(r'href="([^"]*view_detail\.jsp[^"]*)"[^>]*>(.*?)</a>', s, flags=re.S):
        href = html.unescape(href)
        m = re.search(r"dcd=(\d+)", href)
        if not m:
            continue
        items.append({"id": m.group(1), "url": urllib.parse.urljoin(CR_BASE, href),
                      "title": strip_html(title)})
    return items


def cr_press_text(url):
    s = get(url, timeout=60, retries=2).text
    t = strip_html(s)
    i = t.find("Central Railway Press Release")
    return t[i:] if i > 0 else t[-6000:]


def announcement_alert(notifier, cfg, source, title, url, text, published=""):
    score, det = relevance(title + " " + text)
    trains, dates, booking = extract_facts(text)
    prio_tag = date_priority(dates, cfg)
    relevant = det["origin"] and det["dest"]           # mentions Pune AND a Bihar station
    maybe = det["origin"] and det["special"]           # Pune specials, destination not parsed
    maybe |= det["dest"] and det["special"] and ("central railway" in text.lower() or "pune" in title.lower())
    if not (relevant or maybe):
        log.info("[%s] not relevant: %s", source, title[:90])
        return False
    snippet = ""
    for kw in ["patna", "danapur", "muzaffarpur", "पटना", "दानापुर", "मुजफ्फरपुर", "pune", "पुणे"]:
        i = text.lower().find(kw)
        if i >= 0:
            snippet = text[max(0, i - 160): i + 260]
            break
    body = "\n".join([
        f"📢 {title}",
        f"Source: {source}" + (f"  Published: {published}" if published else ""),
        f"Link: {url}",
        f"Special train numbers mentioned: {', '.join(trains) or 'none parsed'}",
        f"Dates mentioned: {', '.join(dates[:12]) or 'none parsed'}" + (f"  ← {prio_tag}" if prio_tag else ""),
        f"Booking info: {' / '.join(booking) or 'not stated'}",
        f"Excerpt: …{snippet}…" if snippet else "",
        f"Book: {irctc_link()}",
        f"Detected: {stamp()}",
    ])
    urgent = relevant and prio_tag.startswith("TARGET")
    notifier.send(("🚨 " if urgent else "📢 ") + f"{source}: {title[:110]}",
                  body, "urgent" if urgent else ("high" if relevant else "default"),
                  ["loudspeaker"])
    return True


def check_cr_press(cfg, state, notifier, baseline=False):
    try:
        items = cr_press_items()
    except Exception as e:
        log.warning("CR press list failed: %s", e)
        return
    known = state.data["press"]
    for it in items:
        if it["id"] in known:
            continue
        known[it["id"]] = {"title": it["title"], "seen": stamp()}
        if baseline:
            log.info("baseline CR press: %s", it["title"][:100])
            continue
        try:
            text = cr_press_text(it["url"])
        except Exception as e:
            log.warning("CR detail fetch failed: %s", e)
            text = ""
        announcement_alert(notifier, cfg, "Central Railway press release", it["title"], it["url"], text)
        time.sleep(1)
    state.save()


# --------------------------------------------------------------------------- #
# Source 3: PIB RSS
# --------------------------------------------------------------------------- #
PIB_RSS = "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"


def rss_items(url):
    r = get(url)
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        # PIB occasionally serves malformed XML; fall back to a lenient regex parse
        out = []
        for block in re.findall(r"<item>(.*?)</item>", r.text, flags=re.S):
            g = lambda tag: html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", (re.search(rf"<{tag}>(.*?)</{tag}>", block, flags=re.S) or [None, ""])[1])).strip()
            out.append({"title": g("title"), "link": g("link"), "guid": g("guid") or g("link"),
                        "pub": g("pubDate"), "source": g("source")})
        return out
    out = []
    for item in root.iter("item"):
        g = lambda tag: (item.findtext(tag) or "").strip()
        out.append({"title": html.unescape(g("title")), "link": g("link"),
                    "guid": g("guid") or g("link"), "pub": g("pubDate"),
                    "source": (item.find("source").text if item.find("source") is not None else "")})
    return out


def check_pib(cfg, state, notifier, baseline=False):
    try:
        items = rss_items(PIB_RSS)
    except Exception as e:
        log.warning("PIB RSS failed: %s", e)
        return
    known = state.data["pib"]
    for it in items:
        m = re.search(r"PRID=(\d+)", it["link"])
        pid = m.group(1) if m else hashlib.md5(it["link"].encode()).hexdigest()
        if pid in known:
            continue
        known[pid] = it["title"][:120]
        if baseline:
            continue
        if not kw_hits(it["title"], KW_RAIL):
            continue
        try:
            text = strip_html(get(it["link"]).text)
        except Exception:
            text = ""
        announcement_alert(notifier, cfg, "PIB (Govt of India)", it["title"], it["link"], text)
        time.sleep(1)
    state.save()


# --------------------------------------------------------------------------- #
# Source 4: Google News RSS
# --------------------------------------------------------------------------- #
def gnews_url(q, hindi=False):
    base = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "hi" if hindi else "en-IN", "gl": "IN", "ceid": "IN:hi" if hindi else "IN:en"})
    return base


def check_news(cfg, state, notifier, baseline=False):
    known = state.data["news"]
    started = dt.datetime.strptime(state.data["started"], "%d-%b-%Y %H:%M:%S IST").replace(tzinfo=IST)
    for q in cfg["news_queries"]:
        hindi = bool(re.search(r"[ऀ-ॿ]", q))
        try:
            items = rss_items(gnews_url(q, hindi))
        except Exception as e:
            log.warning("news query failed (%s): %s", q, e)
            continue
        for it in items:
            key = hashlib.md5((it["guid"] or it["title"]).encode()).hexdigest()
            if key in known:
                continue
            known[key] = it["title"][:120]
            if baseline:
                continue
            # ignore old articles: only items published after the watcher started
            try:
                pub = dt.datetime.strptime(it["pub"], "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=dt.timezone.utc)
                if pub < started - dt.timedelta(hours=12):
                    continue
                pub_s = pub.astimezone(IST).strftime("%d-%b-%Y %H:%M IST")
            except Exception:
                pub_s = it["pub"]
            title = it["title"]
            score, det = relevance(title)
            if det["origin"] and (det["dest"] or det["special"]) or (det["dest"] and det["special"] and "pune" in q.lower()):
                announcement_alert(notifier, cfg, f"News ({it['source'] or 'Google News'})",
                                   title, it["link"], title, pub_s)
        time.sleep(0.7)
    state.save()


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #
def heartbeat(cfg, state, notifier):
    hb = cfg.get("daily_heartbeat_ist")
    if not hb:
        return
    today = now_ist().strftime("%Y-%m-%d")
    if state.data.get("last_heartbeat") == today:
        return
    if now_ist().strftime("%H:%M") >= hb:
        state.data["last_heartbeat"] = today
        specials = [f"{k.split('|')[0]} {v.get('name','')} dates={v.get('dates')}"
                    for k, v in state.data["erail"].items()
                    if k.startswith("0") and not v.get("gone")]
        where = os.environ.get("WATCHER_NAME", "laptop")
        notifier.send(f"Special-train watcher alive ✅ ({where})",
                      f"Still watching Pune → {'/'.join(cfg['destinations'])} for "
                      f"{cfg['target_dates'][0]} to {cfg['target_dates'][-1]}.\n"
                      f"Specials currently listed from Pune: {'; '.join(specials) or 'none'}\n"
                      f"Sources OK as of {stamp()}", "low", ["white_check_mark"])
        state.save()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def one_cycle(cfg, state, notifier, avail, baseline=False):
    check_erail(cfg, state, notifier, avail, baseline)
    if baseline or time.time() - state.data.get("last_press_poll", 0) >= cfg["press_poll_seconds"]:
        check_cr_press(cfg, state, notifier, baseline)
        check_pib(cfg, state, notifier, baseline)
        check_news(cfg, state, notifier, baseline)
        state.data["last_press_poll"] = time.time()
        state.save()
    heartbeat(cfg, state, notifier)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--state", default=str(HERE / "state.json"))
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit (cron / CI mode)")
    ap.add_argument("--max-minutes", type=float, default=0, help="exit after this many minutes (CI chaining)")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("--reset", action="store_true", help="forget state (re-baseline)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(HERE / "watch.log", encoding="utf-8")])
    cfg = load_config(Path(args.config))
    state_path = Path(args.state)
    if args.reset and state_path.exists():
        state_path.unlink()
    state = State(state_path)
    notifier = Notifier(cfg, dry=args.dry_run)
    avail = Availability(cfg)

    if args.test_notify:
        notifier.send("Test: Pune→Bihar special-train watcher",
                      f"Notifications are working. Watching PUNE→PNBE/MFP/DNR for "
                      f"{', '.join(cfg['target_dates'])}.\nDetected: {stamp()}", "default", ["white_check_mark"])
        return

    if not (cfg["notify"]["ntfy_topic"] or cfg["notify"]["telegram_bot_token"]) and not args.dry_run:
        log.warning("No ntfy_topic / telegram configured in config.json – alerts will only be written to alerts.log")
    log.info("watcher start; first_run=%s; availability=%s", state.first_run, "on" if avail.enabled else "off (no RapidAPI key)")

    stop = dt.date.fromisoformat(cfg["stop_after"])
    t0 = time.time()
    while True:
        if args.max_minutes and time.time() - t0 > args.max_minutes * 60:
            log.info("max runtime reached (%.0f min); exiting for the next chained run", args.max_minutes)
            break
        if now_ist().date() > stop:
            log.info("monitoring period ended (%s); nothing to do", cfg["stop_after"])
            if not args.once and not state.data.get("finished"):
                notifier.send("Watcher finished", f"Monitoring period ended ({cfg['stop_after']}).", "low")
                state.data["finished"] = stamp(); state.save()
            break
        try:
            first = state.first_run
            one_cycle(cfg, state, notifier, avail, baseline=first)
            if first:
                log.info("baseline complete: %d trains, %d CR releases, %d PIB, %d news items recorded",
                         len(state.data["erail"]), len(state.data["press"]),
                         len(state.data["pib"]), len(state.data["news"]))
        except KeyboardInterrupt:
            raise
        except Exception:
            log.error("cycle error:\n%s", traceback.format_exc())
        if args.once:
            break
        time.sleep(cfg["poll_seconds"] + random.uniform(0, 15))


if __name__ == "__main__":
    main()
