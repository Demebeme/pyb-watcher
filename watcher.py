"""PYB watcher: sub-hourly alert detection for Pay Your Bills, hosted on GitHub Actions.

Runs every ~5 minutes. Deterministic detection, templated JARVIS-style alerts,
edge-tts voice notes. State persists in the game's Open Cloud datastore.
The hourly cloud routine still owns the guaranteed daily report.
"""
import json, os, random, re, subprocess, sys, tempfile, html
from datetime import datetime, timedelta, timezone

RBX_KEY = os.environ["ROBLOX_KEY"].strip()
TG_TOKEN = os.environ["TG_TOKEN"].strip()
CHAT_ID = "8072365296"
UNIVERSE = "10634123209"
METRICS_URL = f"https://apis.roblox.com/analytics-query-api/v1/universes/{UNIVERSE}/metrics"
DS_BASE = (f"https://apis.roblox.com/datastores/v1/universes/{UNIVERSE}/"
           "standard-datastores/datastore/entries/entry?datastoreName=PYB_StatsBot&entryKey=")
RELEASE_START = "2026-08-17T00:00:00Z"

import requests

NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")
YDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
TOMORROW = (NOW + timedelta(days=1)).strftime("%Y-%m-%d")
ASOF = NOW.strftime("%H:%M UTC")
# vary endTime so the analytics API can't serve a stale cached operation
CACHEBUST = NOW.strftime("T00:%M:%SZ")

def log(*a):
    print(f"[{NOW.isoformat()}]", *a, flush=True)

def metric(name, start, end, breakdown=None, granularity="OneDay"):
    body = {"metric": name, "granularity": granularity, "startTime": start, "endTime": end}
    if breakdown:
        body["breakdown"] = breakdown
    r = requests.post(METRICS_URL, headers={"x-api-key": RBX_KEY}, json=body, timeout=30)
    data = r.json()
    tries = 0
    while not data.get("done") and "path" in data and tries < 6:
        import time; time.sleep(2); tries += 1
        pr = requests.get(f"https://apis.roblox.com/{data['path']}",
                          headers={"x-api-key": RBX_KEY}, timeout=30)
        data = pr.json()
    if "error" in data:
        log(f"metric {name} error: {data['error']}")
        return []
    return (data.get("response") or {}).get("values") or []

def series(values):
    """{date: value} for an un-broken-down metric result."""
    out = {}
    for v in values:
        for dp in v.get("dataPoints", []):
            out[dp["time"][:10]] = dp["value"]
    return out

def funnel(day_start, day_end):
    vals = metric("FunnelUserTotalCount", day_start, day_end,
                  breakdown=["FunnelStep"], granularity="None")
    steps = {}
    for v in vals:
        bd = v.get("breakdowns") or []
        if bd:
            name = bd[0].get("displayValue") or bd[0].get("value")
            pts = v.get("dataPoints") or []
            steps[name] = sum(p["value"] for p in pts)
    return steps

def ds_get(key):
    r = requests.get(DS_BASE + key, headers={"x-api-key": RBX_KEY}, timeout=30)
    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            return None
    return None

def ds_set(key, obj):
    requests.post(DS_BASE + key, headers={"x-api-key": RBX_KEY,
                  "Content-Type": "application/json"}, json=obj, timeout=30)

def tg_text(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "parse_mode": "HTML", "text": text}, timeout=30)
    ok = r.json().get("ok")
    log("sent text" if ok else f"text send failed: {r.text[:200]}")
    return ok

def tg_voice(text):
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    with tempfile.TemporaryDirectory() as td:
        txt, mp3, ogg = (os.path.join(td, n) for n in ("b.txt", "b.mp3", "b.ogg"))
        open(txt, "w", encoding="utf-8").write(plain)
        r = subprocess.run([sys.executable, "-m", "edge_tts", "--voice", "en-GB-RyanNeural",
                            "--rate=+5%", "--pitch=-6Hz", "--file", txt, "--write-media", mp3],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not os.path.exists(mp3):
            log(f"tts failed: {(r.stderr or '')[:200]}"); return
        c = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
                            "-c:a", "libopus", "-b:a", "48k", ogg],
                           capture_output=True, text=True, timeout=120)
        target, field, url = (ogg, "voice", "sendVoice") if c.returncode == 0 else (mp3, "audio", "sendAudio")
        with open(target, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{url}",
                          data={"chat_id": CHAT_ID}, files={field: f}, timeout=120)
        log("sent voice")

SIR = {
    "starts": ["Sir, today is now your biggest day on record.",
               "Sir, the inflow record has fallen."],
    "ccu": ["Sir, more players are in the game right now than ever before.",
            "Sir, a new concurrency high."],
    "homerec": ["Sir, Roblox is showing the game on Home.",
                "Sir, home recommendations are moving."],
    "revenue": ["Sir, money has arrived.",
                "Sir, a purchase has come through."],
    "slowdown": ["Sir, today is running well behind and deserves your attention."],
    "campaign": ["Sir, the ad campaign requires attention."],
}

def alert(tag, kind, trigger_line, context_lines, meaning):
    lines = [f"<b>{tag}</b>", random.choice(SIR[kind]), f"As of {ASOF}.", "", trigger_line]
    lines += context_lines + ["", meaning]
    text = "\n".join(lines)
    if tg_text(text):
        tg_voice(text)
        return True
    return False

def main():
    state = ds_get("PYB_WatcherState")
    first_run = state is None
    if first_run:
        state = {"records": {"peakCcu": 20.38, "bestDayStarts": 1108, "bestQualifiedDay": 487,
                             "homeRecImpr": 2, "totalRevenueSeen": 118},
                 "dayFlags": {"date": TODAY, "beatAlerted": False, "slowdownAlerted": False},
                 "homeRecMilestoneSent": False, "lastUpdateId": 0, "voicedSentAt": ""}
    rec, flags = state["records"], state["dayFlags"]

    # ---- owner messages (HEP reports) ----
    off = state.get("lastUpdateId", 0)
    upd = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
                       + (f"?offset={off + 1}" if off else ""), timeout=30).json()
    for u in upd.get("result", []):
        state["lastUpdateId"] = max(state.get("lastUpdateId", 0), u["update_id"])
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
            continue
        m = re.match(r"^\s*(?:hep\s*[:=]?\s*)?(\d{1,4})(?:\s*(?:/|of|out of)\s*250)?\s*$",
                     (msg.get("text") or ""), re.I)
        if m and not first_run:
            hep = int(m.group(1))
            cloud = ds_get("state") or {}
            cloud["knownHep"], cloud["knownHepDate"] = hep, TODAY
            ds_set("state", cloud)
            tg_text(f"Noted, sir. Highly engaged players: {hep} of 250 (as of today). "
                    f"{250 - hep} to go.")

    # ---- day rollover ----
    if flags.get("date") != TODAY:
        # exact day boundary: a cache-busted endTime would leak today into the window
        y_final = funnel(f"{YDAY}T00:00:00Z", f"{TODAY}T00:00:00Z").get("First Run Started", 0)
        rec["bestDayStarts"] = max(rec.get("bestDayStarts", 0), y_final)
        state["dayFlags"] = flags = {"date": TODAY, "beatAlerted": False, "slowdownAlerted": False}

    # ---- gather ----
    f_today = funnel(f"{TODAY}T00:00:00Z", f"{TOMORROW}{CACHEBUST}")
    # exact end boundary: cache-busting here would leak today's partial data into "yesterday, final"
    f_yday = funnel(f"{YDAY}T00:00:00Z", f"{TODAY}T00:00:00Z")
    starts = f_today.get("First Run Started", 0)
    y_starts = f_yday.get("First Run Started", 0)
    done6 = f_today.get("First Skill Bought", 0)
    comp = round(100 * done6 / starts) if starts else 0

    ccu = series(metric("PeakConcurrentPlayers",
                        f"{YDAY}T00:00:00Z", f"{TOMORROW}{CACHEBUST}"))
    ccu_today = ccu.get(TODAY, 0)

    qual = series(metric("QualifiedUniqueUsersWithPlaySessions",
                         f"{YDAY}T00:00:00Z", f"{TOMORROW}{CACHEBUST}"))
    qual_today = qual.get(TODAY)
    if qual.get(YDAY):
        rec["bestQualifiedDay"] = max(rec.get("bestQualifiedDay", 0), qual[YDAY])

    homerec_total = 0
    for v in metric("UniqueUsersWithImpressions", RELEASE_START, f"{TOMORROW}{CACHEBUST}",
                    breakdown=["AcquisitionSource"]):
        bd = v.get("breakdowns") or []
        if bd and bd[0].get("value") == "HomeRecommendation":
            homerec_total = sum(p["value"] for p in v.get("dataPoints", []))

    rev_daily = sum(series(metric("DailyRevenue", RELEASE_START,
                                  f"{TOMORROW}{CACHEBUST}")).values())
    rev_items = 0
    for v in metric("ItemMonetizationRevenue", RELEASE_START, f"{TOMORROW}{CACHEBUST}",
                    granularity="None"):
        rev_items += sum(p["value"] for p in v.get("dataPoints", []))
    revenue_total = max(rev_daily, rev_items)

    camps = requests.get("https://apis.roblox.com/ads-management/v1/campaigns",
                         headers={"x-api-key": RBX_KEY}, timeout=30).json().get("campaigns", [])
    camp_bad = None
    for c in camps:
        if c.get("targetUniverseId") == UNIVERSE and c.get("status") == "ACTIVE":
            reasons = c.get("deliveryStatusReasons") or []
            if c.get("deliveryStatus") == "NOT_SERVING" and reasons != ["SCHEDULED"]:
                camp_bad = (c.get("name"), "+".join(reasons))

    if first_run:
        # establish baselines quietly; never alert on initialization
        rec["homeRecImpr"] = max(rec["homeRecImpr"], homerec_total)
        rec["totalRevenueSeen"] = max(rec["totalRevenueSeen"], revenue_total)
        rec["peakCcu"] = max(rec["peakCcu"], ccu_today)
        lb = ds_get("lastBriefing") or {}
        state["voicedSentAt"] = lb.get("sentAt", "")
        ds_set("PYB_WatcherState", state)
        log("initialized baseline, no alerts")
        return

    ctx_inflow = [f"Qualified users today so far: {qual_today}" if qual_today else None,
                  f"Onboarding completion today so far: {comp} pct",
                  f"Peak CCU today so far: {round(ccu_today, 1)}",
                  f"Starts yesterday, final: {y_starts:,}"]
    ctx_inflow = [c for c in ctx_inflow if c]

    # ---- triggers ----
    if starts > rec["bestDayStarts"] and not flags["beatAlerted"]:
        if alert("NEW RECORD: DAILY PLAYERS", "starts",
                 f"Starts today so far: {starts:,} (previous record: {rec['bestDayStarts']:,}, full day)",
                 ctx_inflow, "The day is still open; the final number will be higher."):
            flags["beatAlerted"] = True

    if ccu_today > rec["peakCcu"]:
        if alert("NEW RECORD: PEAK CCU", "ccu",
                 f"Peak concurrent players: {round(ccu_today, 1)} (previous record: {round(rec['peakCcu'], 1)})",
                 [f"Starts today so far: {starts:,}",
                  f"Qualified users today so far: {qual_today}" if qual_today else "",
                  f"Onboarding completion today so far: {comp} pct"],
                 "Concurrency records compound the discovery signals."):
            rec["peakCcu"] = ccu_today

    if homerec_total > rec["homeRecImpr"]:
        if alert("HOME RECS MOVEMENT", "homerec",
                 f"Home impressions, lifetime: {homerec_total} (was: {rec['homeRecImpr']})",
                 [f"Starts today so far: {starts:,}",
                  f"Peak CCU today so far: {round(ccu_today, 1)}"],
                 "This is the door you have been waiting on. I will report every movement."):
            rec["homeRecImpr"] = homerec_total
        if homerec_total > 10 and not state.get("homeRecMilestoneSent"):
            tg_text("<b>MILESTONE</b>\nSir, home recommendation impressions have passed 10 lifetime. "
                    "Roblox is testing the game on Home in earnest.")
            state["homeRecMilestoneSent"] = True

    if revenue_total > rec["totalRevenueSeen"]:
        gained = round(revenue_total - rec["totalRevenueSeen"])
        if alert("NEW REVENUE", "revenue",
                 f"New revenue: {gained} Robux (lifetime: {round(revenue_total)} Robux)",
                 [f"Starts today so far: {starts:,}",
                  f"Qualified users today so far: {qual_today}" if qual_today else ""],
                 "A rare event, sir. Worth studying what this buyer did."):
            rec["totalRevenueSeen"] = revenue_total

    if (NOW.hour >= 18 and y_starts and starts < 0.5 * y_starts
            and not flags["slowdownAlerted"]):
        if alert("ATTENTION: SLOWDOWN", "slowdown",
                 f"Starts today so far: {starts:,} (yesterday, final: {y_starts:,})",
                 [f"Pace: under half of yesterday with the day mostly done",
                  f"Peak CCU today so far: {round(ccu_today, 1)}"],
                 "Check the campaign spend and delivery before the day is lost."):
            flags["slowdownAlerted"] = True

    if camp_bad and state.get("campaignNote") != camp_bad[1]:
        alert("ATTENTION: CAMPAIGN", "campaign",
              f"Campaign: {camp_bad[0]}",
              [f"Status: NOT_SERVING ({camp_bad[1]})"],
              "Traffic is off until this is resolved.")
        state["campaignNote"] = camp_bad[1]
    elif not camp_bad:
        state["campaignNote"] = "SERVING"

    # ---- daily rollup fast-poke: fire the report routine the moment yesterday lands ----
    # start is cache-busted (day-before 23:MM varies per run); end stays an exact boundary
    dau_start = (NOW - timedelta(days=2)).strftime("%Y-%m-%d") + NOW.strftime("T23:%M:00Z")
    dau = series(metric("DailyActiveUsers", dau_start, f"{TODAY}T00:00:00Z"))
    if dau.get(YDAY) is not None and state.get("pokedForDay") != YDAY:
        cloud = ds_get("state") or {}
        if cloud.get("lastRolledDay") != YDAY:
            if os.environ.get("GITHUB_ACTIONS"):
                try:
                    open("rollup-marker.txt", "w").write(f"{YDAY} landed at {NOW.isoformat()}\n")
                    for cmd in (["git", "config", "user.name", "pyb-watcher"],
                                ["git", "config", "user.email", "watcher@users.noreply.github.com"],
                                ["git", "add", "rollup-marker.txt"],
                                ["git", "commit", "-m", f"rollup {YDAY} landed"],
                                ["git", "push"]):
                        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    log(f"rollup {YDAY} landed, poked repo to fire the daily report")
                except Exception as e:
                    log(f"poke failed: {e!r}")
            state["pokedForDay"] = YDAY

    # ---- voice the daily report if a new one is stored ----
    lb = ds_get("lastBriefing") or {}
    if lb.get("text") and lb.get("sentAt") and lb["sentAt"] != state.get("voicedSentAt"):
        tg_voice(lb["text"])
        state["voicedSentAt"] = lb["sentAt"]

    ds_set("PYB_WatcherState", state)
    log(f"done: starts={starts} ccu={ccu_today} homerec={homerec_total} rev={revenue_total}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"error: {e!r}")
