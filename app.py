from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from functools import lru_cache
from pathlib import Path
import csv, json, math, os, threading, time
from io import StringIO

app = FastAPI(title="HR Matchup API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TZ = ZoneInfo("America/Chicago")
SESSION = requests.Session()
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/hr_api_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

LOOKBACK_DAYS = int(os.getenv("STATCAST_LOOKBACK_DAYS", "1095"))
CACHE_MAX_AGE_HOURS = int(os.getenv("CACHE_MAX_AGE_HOURS", "36"))

cache_build_lock = threading.Lock()
cache_build_started_at = None
last_cache_error = None

TEAM_ABBR_BY_ID = {
    108: "LAA", 109: "AZ", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC", 119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD", 136: "SEA", 137: "SF", 138: "STL",
    139: "TB", 140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}
TEAM_LOGO_SLUGS = {
    "AZ": "ari", "ATL": "atl", "BAL": "bal", "BOS": "bos", "CHC": "chc", "CWS": "chw",
    "CIN": "cin", "CLE": "cle", "COL": "col", "DET": "det", "HOU": "hou", "KC": "kc",
    "LAA": "laa", "LAD": "lad", "MIA": "mia", "MIL": "mil", "MIN": "min", "NYM": "nym",
    "NYY": "nyy", "ATH": "oak", "OAK": "oak", "PHI": "phi", "PIT": "pit", "SD": "sd",
    "SEA": "sea", "SF": "sf", "STL": "stl", "TB": "tb", "TEX": "tex", "TOR": "tor", "WSH": "wsh",
}

def now_ct():
    return datetime.now(TZ)

def current_season():
    return now_ct().year

def day_str(days_ago=0):
    return (now_ct() - timedelta(days=days_ago)).strftime("%Y-%m-%d")

def safe_float(value, default=0.0):
    try:
        if value in (None, "", "-.--", "null"):
            return default
        n = float(value)
        if math.isnan(n):
            return default
        return n
    except Exception:
        return default

def safe_int(value, default=0):
    try:
        if value in (None, "", "-.--", "null"):
            return default
        return int(float(value))
    except Exception:
        return default

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def scale(v, lo, hi):
    if v is None:
        return 0
    return clamp((safe_float(v) - lo) / (hi - lo), 0, 1)

def get_json(url, timeout=20):
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

def team_abbr(team: dict) -> str:
    team_id = team.get("id")
    abbr = team.get("abbreviation") or TEAM_ABBR_BY_ID.get(team_id)
    return str(abbr).upper() if abbr else "MLB"

def team_logo(abbr: str):
    slug = TEAM_LOGO_SLUGS.get((abbr or "").upper())
    return f"https://a.espncdn.com/i/teamlogos/mlb/500/{slug}.png" if slug else None

def player_headshot(player_id):
    return f"https://img.mlbstatic.com/mlb-photos/image/upload/w_180/v1/people/{player_id}/headshot/67/current" if player_id else None

def normalize_team(team: dict) -> dict:
    abbr = team_abbr(team or {})
    return {**(team or {}), "abbreviation": abbr, "logo": team_logo(abbr)}

def normalize_game(game):
    away_block = ((game.get("teams") or {}).get("away") or {})
    home_block = ((game.get("teams") or {}).get("home") or {})
    away = normalize_team(away_block.get("team", {}) or {})
    home = normalize_team(home_block.get("team", {}) or {})
    away_abbr = away["abbreviation"]
    home_abbr = home["abbreviation"]
    return {
        "gamePk": game.get("gamePk"),
        "gameDate": game.get("gameDate"),
        "status": ((game.get("status") or {}).get("detailedState")) or "Scheduled",
        "away": away,
        "home": home,
        "awayProbablePitcher": away_block.get("probablePitcher"),
        "homeProbablePitcher": home_block.get("probablePitcher"),
        "label": f"{away_abbr} @ {home_abbr}",
        "awayLogo": team_logo(away_abbr),
        "homeLogo": team_logo(home_abbr),
    }

def get_games_raw(date_str):
    try:
        data = get_json(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}&hydrate=probablePitcher")
        games = []
        for db in data.get("dates", []):
            for game in db.get("games", []):
                try:
                    games.append(normalize_game(game))
                except Exception as exc:
                    print(f"[WARN] Failed to normalize game: {exc}")
        return games
    except Exception as exc:
        print(f"[ERROR] get_games_raw failed for {date_str}: {exc}")
        return []

def get_game_by_pk(game_pk: int):
    for url in [
        f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&gamePk={game_pk}&hydrate=probablePitcher",
        f"https://statsapi.mlb.com/api/v1/schedule?gamePk={game_pk}&hydrate=probablePitcher",
    ]:
        try:
            data = get_json(url)
            for db in data.get("dates", []) or []:
                for game in db.get("games", []) or []:
                    if int(game.get("gamePk")) == int(game_pk):
                        return normalize_game(game)
        except Exception:
            pass
    for offset in range(-2, 5):
        try:
            for game in get_games_raw((now_ct() + timedelta(days=offset)).strftime("%Y-%m-%d")):
                if int(game.get("gamePk")) == int(game_pk):
                    return game
        except Exception:
            continue
    return None

@lru_cache(maxsize=256)
def active_roster(team_id: int):
    rows, seen = [], set()
    if not team_id:
        return rows
    for roster_type in ["active", "40Man"]:
        try:
            data = get_json(f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster?rosterType={roster_type}")
        except Exception:
            continue
        for item in data.get("roster", []) or []:
            person = item.get("person") or {}
            pid = person.get("id")
            if not pid or pid in seen:
                continue
            pos = (item.get("position") or {}).get("abbreviation", "")
            if pos == "P":
                continue
            seen.add(pid)
            rows.append({"playerId": pid, "name": person.get("fullName", "Unknown"), "position": pos})
        if rows:
            return rows
    return rows

@lru_cache(maxsize=4096)
def hitter_season_stats(player_id: int, season: int):
    try:
        data = get_json(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&group=hitting&season={season}")
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        return (splits[0].get("stat") or {}) if splits else {}
    except Exception:
        return {}

@lru_cache(maxsize=4096)
def pitcher_season_stats(player_id: int, season: int):
    try:
        data = get_json(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&group=pitching&season={season}")
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        return (splits[0].get("stat") or {}) if splits else {}
    except Exception:
        return {}

def pitcher_hr9(pitcher_id):
    if not pitcher_id:
        return None
    stat = pitcher_season_stats(int(pitcher_id), current_season())
    hr = safe_float(stat.get("homeRuns"), 0)
    ip = safe_float(stat.get("inningsPitched"), 0)
    return round((hr / ip) * 9, 2) if ip > 0 else None

def cache_file():
    return CACHE_DIR / f"statcast_air_v20_{day_str(0)}.json"

def cache_is_fresh():
    path = cache_file()
    if not path.exists():
        return False
    return ((time.time() - path.stat().st_mtime) / 3600) <= CACHE_MAX_AGE_HOURS

def load_cache():
    path = cache_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {int(k): v for k, v in data.get("profiles", {}).items()}
    except Exception:
        return {}

def cache_meta():
    path = cache_file()
    base = {
        "building": cache_build_lock.locked(),
        "buildStartedAt": cache_build_started_at,
        "lastError": last_cache_error,
        "lookbackDays": LOOKBACK_DAYS,
        "note": "v22 kHR includes hitter quality, matchup boost, and recent form; frontend should not double-count Matchup."
    }
    if not path.exists():
        return {**base, "exists": False, "fresh": False}
    try:
        data = json.loads(path.read_text())
        return {
            **base,
            "exists": True,
            "fresh": cache_is_fresh(),
            "ageHours": round((time.time() - path.stat().st_mtime) / 3600, 2),
            "profileCount": len(data.get("profiles", {})),
            "dateRange": data.get("dateRange"),
            "source": data.get("source"),
            "chunksDone": data.get("chunksDone"),
            "chunksTotal": data.get("chunksTotal"),
        }
    except Exception:
        return {**base, "exists": True, "fresh": False, "error": "Could not read cache"}

def month_chunks(start: date, end: date):
    cur = date(start.year, start.month, 1)
    chunks = []
    while cur <= end:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        chunks.append((max(cur, start).strftime("%Y-%m-%d"), min(nxt - timedelta(days=1), end).strftime("%Y-%m-%d")))
        cur = nxt
    return chunks

def savant_csv_rows(start_date, end_date, timeout=75):
    url = "https://baseballsavant.mlb.com/statcast_search/csv"
    params = {
        "all": "true",
        "hfGT": "R|",
        "player_type": "batter",
        "game_date_gt": start_date,
        "game_date_lt": end_date,
        "group_by": "name",
        "min_pitches": "0",
        "min_results": "0",
        "min_pas": "0",
        "type": "details",
    }
    r = SESSION.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    text = r.text
    if "player_name" not in text[:5000]:
        raise RuntimeError(f"Savant CSV unexpected response: {text[:140]}")
    return list(csv.DictReader(StringIO(text)))

def empty_raw():
    return {
        "pitches": 0, "bip": 0, "ev_sum": 0.0, "ev_count": 0, "max_ev": None,
        "la_sum": 0.0, "la_count": 0, "hard_hit": 0, "sweet": 0, "fb": 0,
        "air_bip": 0, "hard_air": 0, "sweet_air": 0, "barrels": 0, "pulled_barrels": 0, "near_hr": 0, "recent_hr": 0,
        "last_hr_ev": None, "xwoba_sum": 0.0, "xwoba_count": 0,
        "xwobacon_sum": 0.0, "xwobacon_count": 0, "swinging_strikes": 0,
        "ab": 0, "h": 0, "tb": 0,
    }

def event_bases(event):
    e = (event or "").lower()
    if e == "single":
        return 1
    if e == "double":
        return 2
    if e == "triple":
        return 3
    if e == "home_run":
        return 4
    return 0

def is_ab_event(event):
    e = (event or "").lower()
    # Statcast pitch rows only have final event on PA-ending pitch.
    # AB events exclude walks/HBP/sac/interference.
    non_ab = {
        "walk", "intent_walk", "hit_by_pitch", "sac_bunt", "sac_fly",
        "catcher_interf", "field_error", "fielders_choice_out"
    }
    if not e:
        return False
    return e not in non_ab

def is_barrel(row, ev, la):
    if str(row.get("launch_speed_angle") or "").strip() == "6":
        return True
    return safe_float(ev) >= 98 and 8 <= safe_float(la, -999) <= 32

def is_pulled_barrel(row, ev, la):
    if not is_barrel(row, ev, la):
        return False
    stand = (row.get("stand") or "").upper()
    hc_x = safe_float(row.get("hc_x"), None)
    if hc_x is not None and stand in {"R", "L"}:
        return hc_x <= 105 if stand == "R" else hc_x >= 145
    return safe_float(ev) >= 100 and 15 <= safe_float(la, -999) <= 32

def add_row(p, row):
    p["pitches"] += 1

    desc = (row.get("description") or "").lower()
    if "swinging_strike" in desc:
        p["swinging_strikes"] += 1

    event = (row.get("events") or "").lower()
    if is_ab_event(event):
        p["ab"] += 1
        bases = event_bases(event)
        if bases:
            p["h"] += 1
            p["tb"] += bases

    ev = safe_float(row.get("launch_speed"), None)
    la = safe_float(row.get("launch_angle"), None)
    dist = safe_float(row.get("hit_distance_sc"), None)
    xwoba = safe_float(row.get("estimated_woba_using_speedangle"), None)

    if xwoba is not None:
        p["xwoba_sum"] += xwoba
        p["xwoba_count"] += 1

    if ev is not None and la is not None:
        p["bip"] += 1
        p["ev_sum"] += ev
        p["ev_count"] += 1
        p["max_ev"] = ev if p["max_ev"] is None else max(p["max_ev"], ev)
        p["la_sum"] += la
        p["la_count"] += 1
        # v20 quality metrics: closer to the reference dashboard.
        # Quality rates use air/competitive contact instead of every ground ball.
        is_air = la >= 10
        is_fb = la >= 25
        if is_air:
            p["air_bip"] += 1
        if ev >= 95:
            p["hard_hit"] += 1
            if is_air:
                p["hard_air"] += 1
        if 8 <= la <= 32:
            p["sweet"] += 1
            if is_air:
                p["sweet_air"] += 1
        if is_fb:
            p["fb"] += 1
        if is_barrel(row, ev, la):
            p["barrels"] += 1
            # pulled barrel is only useful if it is airborne / HR-shaped.
            if is_air and is_pulled_barrel(row, ev, la):
                p["pulled_barrels"] += 1
        if event == "home_run":
            p["recent_hr"] += 1
            p["last_hr_ev"] = ev
        elif ev >= 102 and 22 <= la <= 38 and dist is not None and dist >= 375:
            p["near_hr"] += 1
        if xwoba is not None:
            p["xwobacon_sum"] += xwoba
            p["xwobacon_count"] += 1

def finalize(p):
    bip, pitches = p["bip"], p["pitches"]
    ab, h, tb = p["ab"], p["h"], p["tb"]
    avg = (h / ab) if ab else None
    slg = (tb / ab) if ab else None
    iso = (slg - avg) if ab and avg is not None and slg is not None else None

    xwoba = round(p["xwoba_sum"] / p["xwoba_count"], 3) if p["xwoba_count"] else None
    xwobacon = round(p["xwobacon_sum"] / p["xwobacon_count"], 3) if p["xwobacon_count"] else None

    # Reference appears lower than raw Savant in some samples; light shrink toward league avg stabilizes.
    if xwoba is not None:
        xwoba = round((xwoba * 0.85) + (0.315 * 0.15), 3)
    if xwobacon is not None:
        xwobacon = round((xwobacon * 0.85) + (0.370 * 0.15), 3)

    return {
        "pitches": pitches,
        "BIP": bip,
        "AB_statcast": ab,
        "H_statcast": h,
        "TB_statcast": tb,
        "ISO_statcast": round(iso, 3) if iso is not None else None,
        "avgEV": round(p["ev_sum"] / p["ev_count"], 1) if p["ev_count"] else None,
        "maxEV": round(p["max_ev"], 1) if p["max_ev"] is not None else None,
        "LA": round(p["la_sum"] / p["la_count"], 1) if p["la_count"] else None,
        # Display metrics are intentionally air-contact focused, matching the reference table better.
        "HH": round((p["hard_air"] / p["air_bip"]) * 100, 1) if p.get("air_bip") else (round((p["hard_hit"] / bip) * 100, 1) if bip else None),
        "sweetSpot": round((p["sweet_air"] / p["air_bip"]) * 100, 1) if p.get("air_bip") else (round((p["sweet"] / bip) * 100, 1) if bip else None),
        "FB": round((p["fb"] / p["air_bip"]) * 100, 1) if p.get("air_bip") else (round((p["fb"] / bip) * 100, 1) if bip else None),
        "brlBip": round((p["barrels"] / bip) * 100, 1) if bip else None,
        "pulledBrl": round((p["pulled_barrels"] / p["air_bip"]) * 100, 1) if p.get("air_bip") else (round((p["pulled_barrels"] / bip) * 100, 1) if bip else None),
        "nearHR": p["near_hr"],
        "recentHR": p["recent_hr"],
        "lastHREV": round(p["last_hr_ev"], 1) if p["last_hr_ev"] is not None else None,
        "xwOBA": xwoba,
        "xwOBAcon": xwobacon,
        "swStr": round((p["swinging_strikes"] / pitches) * 100, 1) if pitches else None,
    }

def save_partial(raw, start_date, end_date, chunks_done, chunks_total):
    profiles = {pid: finalize(p) for pid, p in raw.items()}
    payload = {
        "source": "Baseball Savant Statcast long cache, v20 air-contact metrics",
        "generatedAt": datetime.now(TZ).isoformat(),
        "dateRange": {"start": start_date, "end": end_date},
        "chunksDone": chunks_done,
        "chunksTotal": chunks_total,
        "profiles": {str(k): v for k, v in profiles.items()},
    }
    cache_file().write_text(json.dumps(payload))

def build_cache():
    global cache_build_started_at, last_cache_error
    if not cache_build_lock.acquire(blocking=False):
        return {"ok": False, "message": "Already building"}

    cache_build_started_at = datetime.now(TZ).isoformat()
    last_cache_error = None
    try:
        end = (now_ct() - timedelta(days=1)).date()
        start = end - timedelta(days=LOOKBACK_DAYS)
        chunks = month_chunks(start, end)
        raw = {}
        done = 0
        for a, b in chunks:
            rows = savant_csv_rows(a, b)
            for row in rows:
                batter_id = safe_int(row.get("batter"), 0)
                if batter_id:
                    add_row(raw.setdefault(batter_id, empty_raw()), row)
            done += 1
            save_partial(raw, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), done, len(chunks))
        return {"ok": True, "profileCount": len(raw), "chunksDone": done, "chunksTotal": len(chunks)}
    except Exception as exc:
        last_cache_error = str(exc)
        return {"ok": False, "error": str(exc)}
    finally:
        cache_build_lock.release()

def ensure_cache_background():
    if cache_is_fresh() or cache_build_lock.locked():
        return False
    threading.Thread(target=build_cache, daemon=True).start()
    return True

def season_iso(stat):
    ab = safe_float(stat.get("atBats"), 0)
    if not ab:
        return None
    hits = safe_float(stat.get("hits"), 0)
    doubles = safe_float(stat.get("doubles"), 0)
    triples = safe_float(stat.get("triples"), 0)
    hr = safe_float(stat.get("homeRuns"), 0)
    tb = safe_float(stat.get("totalBases"), hits + doubles + (2 * triples) + (3 * hr))
    return round((tb / ab) - (hits / ab), 3)

def fallback_profile(stat):
    ab = safe_float(stat.get("atBats"), 0)
    pa = safe_float(stat.get("plateAppearances"), 0)
    hr = safe_float(stat.get("homeRuns"), 0)
    hits = safe_float(stat.get("hits"), 0)
    doubles = safe_float(stat.get("doubles"), 0)
    triples = safe_float(stat.get("triples"), 0)
    so = safe_float(stat.get("strikeOuts"), 0)
    tb = safe_float(stat.get("totalBases"), hits + doubles + (2 * triples) + (3 * hr))
    iso = ((tb / ab) - (hits / ab)) if ab else 0
    hr_rate = (hr / ab) if ab else 0
    brl = min(20, max(2.5, iso * 48))
    return {
        "pitches": safe_int(stat.get("numberOfPitches"), 0),
        "BIP": max(0, int(ab - so)),
        "ISO_statcast": None,
        "HH": round(min(58, max(24, 30 + iso * 90)), 1),
        "FB": round(min(50, max(15, 20 + hr_rate * 180)), 1),
        "brlBip": round(brl, 1),
        "sweetSpot": round(min(46, max(27, 32 + iso * 38)), 1),
        "pulledBrl": round(min(12, max(1.8, brl * 0.58)), 1),
        "LA": round(min(25, max(9, 11 + iso * 42)), 1),
        "nearHR": 0,
        "recentHR": 0,
        "maxEV": None,
        "lastHREV": None,
        "xwOBA": None,
        "xwOBAcon": None,
        "swStr": round((so / pa) * 100, 1) if pa else None,
    }

def calibrated_scores(stat, profile, opp_hr9, cache_hit):
    iso = profile.get("ISO_statcast") if cache_hit and profile.get("ISO_statcast") is not None else season_iso(stat)
    iso = iso or 0

    hr = safe_float(stat.get("homeRuns"), 0)
    ab = safe_float(stat.get("atBats"), 0)
    hr_rate = (hr / ab) if ab else 0
    slg = safe_float(stat.get("slg"), 0)
    ops = safe_float(stat.get("ops"), 0)

    brl = safe_float(profile.get("brlBip"), 0)
    pulled = safe_float(profile.get("pulledBrl"), 0)
    hh = safe_float(profile.get("HH"), 0)
    fb = safe_float(profile.get("FB"), 0)
    sweet = safe_float(profile.get("sweetSpot"), 0)
    la = safe_float(profile.get("LA"), 0)
    max_ev = safe_float(profile.get("maxEV"), 0)
    xwoba = profile.get("xwOBA")
    xwobacon = profile.get("xwOBAcon")
    swstr = safe_float(profile.get("swStr"), 10)
    p_hr9 = safe_float(opp_hr9, 1.0)

    recent_hr = safe_float(profile.get("recentHR"), 0)
    near_hr = safe_float(profile.get("nearHR"), 0)

    # v22 kHR formula:
    # kHR now includes matchup + recent form/hot bat, so the frontend should not
    # separately double-count Matchup in Best Matchups.
    contact_score = 0
    contact_score += scale(xwobacon, .320, .500) * 26
    contact_score += scale(xwoba, .300, .450) * 12
    contact_score += scale(brl, 3, 18) * 18
    contact_score += scale(pulled, 1, 12) * 10
    contact_score += scale(hh, 32, 62) * 11
    contact_score += scale(iso, .070, .280) * 8
    contact_score += scale(max_ev, 96, 116) * 5
    contact_score += scale(sweet, 26, 45) * 4
    contact_score += scale(fb, 20, 55) * 3

    # Matchup boost from pitcher HR risk. Conservative, so weak pitchers help but do not dominate.
    matchup_boost = 0
    matchup_boost += max(0, p_hr9 - .80) * 5.5
    matchup_boost = min(matchup_boost, 8)

    # Recent form / hot bat boost from Statcast cache.
    # This stays small because HR props can get noisy fast.
    form_boost = 0
    form_boost += min(5, recent_hr * 1.4)
    form_boost += min(4, near_hr * 0.9)

    # Elite profile combo boosts. This helps Yordan/Judge/Ohtani type hitters separate.
    elite_boost = 0
    if brl >= 14 and safe_float(xwobacon, 0) >= .420:
        elite_boost += 5
    if hh >= 55 and safe_float(xwobacon, 0) >= .430:
        elite_boost += 4
    if brl >= 17:
        elite_boost += 3
    if pulled >= 10 and brl >= 14:
        elite_boost += 2

    swstr_penalty = scale(swstr, 10, 19) * 8

    raw_khr = 24 + contact_score + matchup_boost + form_boost + elite_boost - swstr_penalty

    bip = safe_float(profile.get("BIP"), 0)
    confidence = clamp(bip / 425, 0.72 if cache_hit else 0.65, 1)
    khr = round(clamp((raw_khr * confidence) + (40 * (1 - confidence)), 5, 84), 3)

    # Matchup column = how good the hitter's profile fits the opposing pitcher.
    # It is related to kHR but not identical.
    matchup = round(clamp(
        28
        + scale(xwobacon, .330, .480) * 20
        + scale(brl, 3, 18) * 16
        + scale(pulled, 1, 12) * 8
        + matchup_boost * 2.2
        - scale(swstr, 10, 19) * 6,
        0, 90
    ), 3)

    # Test Score = hitter-only contact quality test.
    test_score = round(clamp(
        26
        + scale(xwobacon, .320, .500) * 24
        + scale(brl, 3, 18) * 20
        + scale(hh, 32, 62) * 12
        + scale(iso, .070, .280) * 6
        - scale(swstr, 10, 19) * 7,
        0, 90
    ), 3)

    # Ceiling = upside if he connects.
    ceiling = round(clamp(
        18
        + scale(max_ev, 96, 116) * 18
        + scale(brl, 3, 18) * 22
        + scale(hh, 32, 62) * 18
        + scale(xwobacon, .320, .500) * 22
        + scale(pulled, 1, 12) * 5
        + scale(iso, .070, .280) * 4,
        8, 99
    ), 3)

    zone_fit = round(clamp(
        0.026
        + (brl * 0.0019)
        + (pulled * 0.0018)
        + scale(xwobacon, .340, .480) * 0.022
        + (0.006 if 12 <= la <= 28 else 0),
        0.020, 0.160
    ), 3)

    # Likely = probability-like output, intentionally lower/tighter than kHR.
    likely = round(clamp((khr * 0.50) + scale(xwobacon, .340, .480) * 10 - scale(swstr, 10, 19) * 7, 1, 65), 0)

    fallback_xwoba = round(max(0.250, min(0.450, 0.260 + (ops * 0.12) + (iso * 0.25))), 3) if ab else None
    fallback_xwobacon = round(max(0.280, min(0.500, 0.300 + (slg * 0.18) + (iso * 0.30))), 3) if ab else None

    return {
        "ISO": round(iso, 3) if iso is not None else None,
        "xwOBA": xwoba if xwoba is not None else fallback_xwoba,
        "xwOBAcon": xwobacon if xwobacon is not None else fallback_xwobacon,
        "matchup": matchup,
        "testScore": test_score,
        "ceiling": ceiling,
        "zoneFit": zone_fit,
        "kHR": khr,
        "likely": likely,
    }

def hr_form(stat, profile, cache_hit):
    if cache_hit:
        swstr = safe_float(profile.get("swStr"), 10)
        brl = safe_float(profile.get("brlBip"), 0)
        score = clamp(34 + brl * 1.2 - scale(swstr, 10, 18) * 12, 18, 78)
        arrow = "↑" if score >= 55 else ("→" if score >= 42 else "↓")
        return f"{int(score)}% {arrow}"
    ab = safe_float(stat.get("atBats"), 0)
    hr = safe_float(stat.get("homeRuns"), 0)
    rate = hr / ab if ab else 0
    score = clamp(28 + rate * 520 + min(12, hr * .35), 18, 74)
    arrow = "↑" if rate >= .055 else ("→" if rate >= .030 else "↓")
    return f"{int(score)}% {arrow}"

def hitter_row(player, team, opp_pitcher, profiles):
    pid = player.get("playerId")
    stat = hitter_season_stats(int(pid), current_season()) if pid else {}
    cache_hit = bool(pid and int(pid) in profiles)
    profile = profiles.get(int(pid)) if cache_hit else fallback_profile(stat)
    opp_hr9 = pitcher_hr9(opp_pitcher.get("id")) if opp_pitcher else None
    scores = calibrated_scores(stat, profile, opp_hr9, cache_hit)

    pa = safe_int(stat.get("plateAppearances"), 0)
    so = safe_int(stat.get("strikeOuts"), 0)
    return {
        "playerId": pid,
        "name": player.get("name", "Unknown"),
        "team": team.get("abbreviation", "MLB"),
        "teamLogo": team_logo(team.get("abbreviation", "MLB")),
        "headshot": player_headshot(pid),
        "pitcher": (opp_pitcher or {}).get("fullName") or (opp_pitcher or {}).get("name") or "TBD",
        "AB": safe_int(stat.get("atBats"), 0),
        "PA": pa,
        "H": safe_int(stat.get("hits"), 0),
        "HR": safe_int(stat.get("homeRuns"), 0),
        "RBI": safe_int(stat.get("rbi"), 0),
        "BB": safe_int(stat.get("baseOnBalls"), 0),
        "SO": so,
        "pitches": profile.get("pitches") or safe_int(stat.get("numberOfPitches"), 0),
        "BIP": profile.get("BIP"),
        "ISO": scores["ISO"],
        "xwOBA": scores["xwOBA"],
        "xwOBAcon": scores["xwOBAcon"],
        "swStr": profile.get("swStr") if profile.get("swStr") is not None else (round((so / pa) * 100, 1) if pa else None),
        "pulledBrl": profile.get("pulledBrl"),
        "brlBip": profile.get("brlBip"),
        "sweetSpot": profile.get("sweetSpot"),
        "FB": profile.get("FB"),
        "HH": profile.get("HH"),
        "LA": profile.get("LA"),
        "nearHR": safe_int(profile.get("nearHR"), 0),
        "maxEV": profile.get("maxEV"),
        "lastHREV": profile.get("lastHREV"),
        "matchup": scores["matchup"],
        "testScore": scores["testScore"],
        "ceiling": scores["ceiling"],
        "zoneFit": scores["zoneFit"],
        "hrForm": hr_form(stat, profile, cache_hit),
        "kHR": scores["kHR"],
        "likely": scores["likely"],
        "status": "Statcast ISO cache" if cache_hit else "Season fallback",
        "cacheHit": cache_hit,
    }

def collect_hitter_rows(game_pk: int):
    ensure_cache_background()
    game = get_game_by_pk(game_pk)
    if not game:
        return []
    away = normalize_team(game["away"])
    home = normalize_team(game["home"])
    away_pitcher = game.get("awayProbablePitcher") or {}
    home_pitcher = game.get("homeProbablePitcher") or {}
    profiles = load_cache()

    rows = []
    for p in active_roster(int(away.get("id") or 0)):
        rows.append(hitter_row(p, away, home_pitcher, profiles))
    for p in active_roster(int(home.get("id") or 0)):
        rows.append(hitter_row(p, home, away_pitcher, profiles))
    rows.sort(key=lambda r: (-safe_float(r.get("kHR")), r.get("team", ""), r.get("name", "")))
    return rows



def pitcher_skill_profile(pitcher_id):
    if not pitcher_id:
        return None
    stat = pitcher_season_stats(int(pitcher_id), current_season())
    if not stat:
        return None

    ip = safe_float(stat.get("inningsPitched"), 0)
    bf = safe_float(stat.get("battersFaced"), 0)
    so = safe_float(stat.get("strikeOuts"), 0)
    bb = safe_float(stat.get("baseOnBalls"), 0)
    hr = safe_float(stat.get("homeRuns"), 0)
    hits = safe_float(stat.get("hits"), 0)

    k_pct = (so / bf) * 100 if bf else 0
    bb_pct = (bb / bf) * 100 if bf else 0
    hr9 = (hr / ip) * 9 if ip else 0
    era = safe_float(stat.get("era"), 0)
    whip = safe_float(stat.get("whip"), 0)

    siera = 4.20 - (k_pct - 20) * 0.055 + (bb_pct - 8) * 0.075 + max(0, hr9 - 1.0) * 0.45
    siera = clamp((siera * 0.70) + (era * 0.30 if era else siera), 2.10, 7.50)

    xwoba = clamp(0.335 - (k_pct - 20) * 0.0035 + (bb_pct - 8) * 0.0025 + (hr9 - 1.0) * 0.025, 0.230, 0.430)
    csw = clamp(27 + (k_pct - 20) * 0.45 - (bb_pct - 8) * 0.18, 20, 36)
    swstr = clamp(10 + (k_pct - 20) * 0.28, 6, 18)
    ball = clamp(34 + (bb_pct - 8) * 0.75, 27, 42)

    brl = clamp(6.5 + (hr9 - 1.0) * 2.8 - (k_pct - 20) * 0.05, 2.0, 15.0)
    pulled = clamp(brl * 0.55 + (hr9 - 1.0) * 1.2, 0.5, 10.0)
    fb = clamp(26 + (hr9 - 1.0) * 5.5, 12, 45)
    hh = clamp(39 + (hits / max(ip, 1) - 1.0) * 8 + (hr9 - 1.0) * 4, 25, 58)

    pitch_score = clamp(
        50 + (k_pct - 22) * 0.9 + (30 - xwoba * 100) * 0.9 + (csw - 28) * 1.2
        - (siera - 4.0) * 3.2 - max(0, hr9 - 1.0) * 3.0,
        5, 80
    )
    strikeout_score = clamp(45 + (k_pct - 20) * 1.25 + (swstr - 10) * 1.5, 5, 80)

    weak_score = clamp(
        50 + (xwoba - .315) * 95 + (brl - 7) * 2.2 + (hh - 40) * 0.8
        + (fb - 28) * 0.45 + max(0, hr9 - 1.0) * 8 - (swstr - 10) * 1.3,
        5, 90
    )

    return {
        "pitcherId": pitcher_id,
        "pitchScore": round(pitch_score, 1),
        "strikeoutScore": round(strikeout_score, 1),
        "weakScore": round(weak_score, 1),
        "xwOBA": round(xwoba, 3),
        "CSW": round(csw, 1),
        "swStr": round(swstr, 1),
        "ball": round(ball, 1),
        "SIERA": round(siera, 1),
        "pulledBrl": round(pulled, 1),
        "brlBip": round(brl, 1),
        "FB": round(fb, 1),
        "HH": round(hh, 1),
        "KPercent": round(k_pct, 1),
        "BBPercent": round(bb_pct, 1),
        "HR9": round(hr9, 2),
        "ERA": era,
        "WHIP": whip,
    }

@app.get("/api/pitchers")
@app.get("/pitchers")
def pitcher_report():
    ensure_cache_background()
    games_today = get_games_raw(day_str(0))
    rows = []

    for game in games_today:
        away = game.get("away") or {}
        home = game.get("home") or {}
        away_p = game.get("awayProbablePitcher") or {}
        home_p = game.get("homeProbablePitcher") or {}

        items = [
            ("away", away_p, away, home),
            ("home", home_p, home, away),
        ]
        for side, pitcher, team_obj, opp_obj in items:
            pid = pitcher.get("id") if isinstance(pitcher, dict) else None
            prof = pitcher_skill_profile(pid)
            if not prof:
                continue
            team_ab = team_obj.get("abbreviation") or team_obj.get("name") or "TEAM"
            opp_ab = opp_obj.get("abbreviation") or opp_obj.get("name") or "TEAM"
            rows.append({
                **prof,
                "gamePk": game.get("gamePk"),
                "gameDate": game.get("gameDate"),
                "team": team_ab,
                "opponent": opp_ab,
                "teamLogo": team_logo(team_ab),
                "opponentLogo": team_logo(opp_ab),
                "pitcher": pitcher.get("fullName") or pitcher.get("name") or "TBD",
                "matchup": f"{team_ab} @ {opp_ab}" if side == "away" else f"{opp_ab} @ {team_ab}",
            })

    top = sorted(rows, key=lambda r: -safe_float(r.get("pitchScore")))
    weak = sorted(rows, key=lambda r: -safe_float(r.get("weakScore")))

    return {
        "date": day_str(0),
        "count": len(rows),
        "source": "Pitcher season skills + estimated contact allowed profile",
        "pitchers": rows,
        "topSlatePitchers": top,
        "weakPitchers": weak,
    }

@app.get("/")
def root():
    ensure_cache_background()
    return {"status": "ok", "message": "HR API v23 games endpoint fix + kHR formula", "cache": cache_meta()}

@app.get("/games")
@app.get("/api/games")
def games():
    try:
        ensure_cache_background()
    except Exception as exc:
        print(f"[WARN] ensure_cache_background failed: {exc}")
    d = day_str(0)
    try:
        games_list = get_games_raw(d)
    except Exception as exc:
        print(f"[ERROR] /api/games failed: {exc}")
        games_list = []
    return {"date": d, "games": games_list, "count": len(games_list), "cache": cache_meta()}

@app.get("/game/{game_pk}")
@app.get("/api/game/{game_pk}")
def game_detail(game_pk: int):
    hitters = collect_hitter_rows(game_pk)
    return {
        "gamePk": game_pk,
        "count": len(hitters),
        "cacheHits": sum(1 for h in hitters if h.get("cacheHit")),
        "cache": cache_meta(),
        "source": "v22 kHR includes matchup + recent form + Statcast event ISO",
        "hitters": hitters,
    }

@app.get("/api/cache/build")
@app.post("/api/cache/build")
def cache_build():
    return {**build_cache(), "cache": cache_meta()}

@app.get("/api/cache/build-background")
@app.post("/api/cache/build-background")
def cache_build_background():
    started = ensure_cache_background()
    return {"ok": True, "started": started, "cache": cache_meta()}

@app.get("/api/cache/status")
def cache_status():
    ensure_cache_background()
    return cache_meta()

@app.get("/api/debug/player/{player_id}")
def debug_player(player_id: int):
    profiles = load_cache()
    return {
        "playerId": player_id,
        "cacheHit": player_id in profiles,
        "statcastProfile": profiles.get(player_id),
        "seasonStats": hitter_season_stats(player_id, current_season()),
        "cache": cache_meta(),
    }
