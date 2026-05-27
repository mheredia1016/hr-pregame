import json
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

API_BASE = os.getenv("HR_API_BASE", "https://hr-api-production-fed2.up.railway.app").rstrip("/")
WEBHOOK_URL = os.getenv("HR_PREGAME_WEBHOOK_URL", "").strip()

TZ = ZoneInfo("America/Chicago")

ALLOWED_START_HOUR = 10
ALLOWED_END_HOUR = 23

WINDOW_MINUTES = int(os.getenv("PREGAME_WINDOW_MINUTES", "60"))
WINDOW_GRACE_MINUTES = int(os.getenv("PREGAME_GRACE_MINUTES", "20"))
STATE_FILE = Path(os.getenv("PREGAME_STATE_FILE", "/tmp/hr_pregame_posted_games.json"))

MIN_HR_SCORE = float(os.getenv("MIN_HR_SCORE", "45"))
TOP_PER_TEAM = int(os.getenv("TOP_PER_TEAM", "3"))

def safe_float(v, default=0.0):
    try:
        if v is None or v == "" or v == "—":
            return default
        return float(str(v).replace("%", "").replace(",", ""))
    except Exception:
        return default

def fmt_num(v, digits=1):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)

def parse_game_time(game_date):
    if not game_date:
        return None
    try:
        return datetime.fromisoformat(game_date.replace("Z", "+00:00")).astimezone(TZ)
    except Exception:
        return None

def fmt_game_time(game_date):
    dt = parse_game_time(game_date)
    return dt.strftime("%-I:%M %p CT") if dt else "Time TBD"

def get_json(path):
    r = requests.get(f"{API_BASE}{path}", timeout=45)
    r.raise_for_status()
    return r.json()

def post_discord(content=None, embeds=None):
    if not WEBHOOK_URL:
        raise RuntimeError("Missing HR_PREGAME_WEBHOOK_URL env var")
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()

def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"posted": {}}

def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        print(f"[WARN] Could not save state: {e}")

def cleanup_state(state):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    posted = state.get("posted", {})
    state["posted"] = {k: v for k, v in posted.items() if str(v).startswith(today)}
    return state

def already_posted(state, game_pk):
    return str(game_pk) in state.get("posted", {})

def mark_posted(state, game_pk):
    state.setdefault("posted", {})[str(game_pk)] = datetime.now(TZ).isoformat()

def game_in_window(game):
    dt = parse_game_time(game.get("gameDate"))
    if not dt:
        return False
    now = datetime.now(TZ)
    start = now + timedelta(minutes=WINDOW_MINUTES - WINDOW_GRACE_MINUTES)
    end = now + timedelta(minutes=WINDOW_MINUTES + WINDOW_GRACE_MINUTES)
    return start <= dt <= end

def pitcher_key(row):
    return str(row.get("pitcher") or "").strip().lower()

def build_pitcher_map():
    try:
        data = get_json("/api/pitchers")
        rows = data.get("pitchers", [])
        return {pitcher_key(r): r for r in rows if pitcher_key(r)}
    except Exception as e:
        print(f"[WARN] Could not load pitcher report: {e}")
        return {}

def hitter_hr_score(h, pitcher=None):
    khr = safe_float(h.get("kHR"))
    xwobacon = safe_float(h.get("xwOBAcon"))
    iso = safe_float(h.get("ISO"))
    hh = safe_float(h.get("HH"))
    swstr = safe_float(h.get("swStr"))

    p_hh = safe_float((pitcher or {}).get("HH"))
    p_fb = safe_float((pitcher or {}).get("FB"))
    p_brl = safe_float((pitcher or {}).get("brlBip"))
    p_xwoba = safe_float((pitcher or {}).get("xwOBA"))

    score = 0
    score += khr * 0.38
    score += (xwobacon * 100) * 0.25
    score += (iso * 100) * 0.18
    score += hh * 0.12

    # Pitcher boost: pitchers allowing HR-shaped contact.
    score += max(0, p_hh - 38) * 0.18
    score += max(0, p_fb - 26) * 0.12
    score += max(0, p_brl - 6) * 0.55
    score += max(0, (p_xwoba - .315) * 100) * 0.25

    # Avoid huge whiff profiles unless the contact quality is elite.
    score -= max(0, swstr - 13) * 0.65

    return round(score, 1)

def tag_for_score(score):
    if score >= 65:
        return "🔥 Best"
    if score >= 56:
        return "💣 Strong"
    if score >= 48:
        return "🎯 Live"
    return "👀 Lean"

def team_rows(rows, team):
    t = str(team).upper()
    return [r for r in rows if str(r.get("team", "")).upper() == t]

def sort_hitters(rows, pitcher_map):
    scored = []
    for h in rows:
        p = pitcher_map.get(str(h.get("pitcher", "")).strip().lower())
        s = hitter_hr_score(h, p)
        scored.append((s, h, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored

def line_for_hitter(rank, score, h, p):
    pitcher_name = h.get("pitcher") or "TBD"

    if p:
        p_bits = (
            f"\n   vs {pitcher_name}: "
            f"HH allowed {fmt_num(p.get('HH'))}% | "
            f"FB {fmt_num(p.get('FB'))}% | "
            f"Brl/BIP {fmt_num(p.get('brlBip'))}% | "
            f"xwOBA {fmt_num(p.get('xwOBA'), 3)}"
        )
    else:
        p_bits = f"\n   vs {pitcher_name}: pitcher risk unavailable"

    return (
        f"**{rank}. {h.get('name','Unknown')}** — {tag_for_score(score)} `{score}`\n"
        f"   kHR {fmt_num(h.get('kHR'))} | "
        f"xwOBAcon {fmt_num(h.get('xwOBAcon'),3)} | "
        f"ISO {fmt_num(h.get('ISO'),3)} | "
        f"HH {fmt_num(h.get('HH'))}% | "
        f"SwStr {fmt_num(h.get('swStr'))}%"
        f"{p_bits}"
    )

def best_two_man(scored_away, scored_home):
    pool = [x for x in (scored_away[:3] + scored_home[:3]) if x[0] >= MIN_HR_SCORE]
    if len(pool) < 2:
        pool = scored_away[:2] + scored_home[:2]
    pool = sorted(pool, key=lambda x: x[0], reverse=True)
    if len(pool) < 2:
        return None
    return pool[0], pool[1], round(pool[0][0] + pool[1][0], 1)

def make_game_embed(game, hitters, pitcher_map):
    away = game.get("away", {}).get("abbreviation", "AWAY")
    home = game.get("home", {}).get("abbreviation", "HOME")
    label = game.get("label") or f"{away} @ {home}"
    game_time = fmt_game_time(game.get("gameDate"))

    away_scored = sort_hitters(team_rows(hitters, away), pitcher_map)
    home_scored = sort_hitters(team_rows(hitters, home), pitcher_map)

    desc = f"**{label} — {game_time}**\n"
    desc += f"Posting ~{WINDOW_MINUTES} minutes before first pitch.\n\n"

    desc += f"__**{away} Top HR Targets**__\n"
    desc += "\n".join(line_for_hitter(i + 1, s, h, p) for i, (s, h, p) in enumerate(away_scored[:TOP_PER_TEAM])) or "No hitters found."
    desc += "\n\n"

    desc += f"__**{home} Top HR Targets**__\n"
    desc += "\n".join(line_for_hitter(i + 1, s, h, p) for i, (s, h, p) in enumerate(home_scored[:TOP_PER_TEAM])) or "No hitters found."
    desc += "\n\n"

    parlay = best_two_man(away_scored, home_scored)
    if parlay:
        a, b, combined = parlay
        desc += (
            "💰 **Possible 2-Man HR Parlay**\n"
            f"{a[1].get('name')} + {b[1].get('name')}\n"
            f"Combined HR Score: `{combined}`"
        )

    return {
        "title": "🚨 1-Hour Pre-Game HR Targets",
        "description": desc[:4000],
        "color": 15158332,
        "footer": {
            "text": "Score = kHR + xwOBAcon + ISO + hitter HH% + pitcher HH/FB/Brl risk - SwStr penalty"
        }
    }

def main():
    now_ct = datetime.now(TZ)

    # Only run between 10 AM and 11:59 PM CT
    if now_ct.hour < ALLOWED_START_HOUR or now_ct.hour > ALLOWED_END_HOUR:
        print("[INFO] Outside allowed hours. Skipping.")
        return

    state = cleanup_state(load_state())

    games_data = get_json("/api/games")
    games = games_data.get("games", games_data if isinstance(games_data, list) else [])

    games_to_post = [
        g for g in games
        if g.get("gamePk")
        and game_in_window(g)
        and not already_posted(state, g.get("gamePk"))
    ]

    print(f"[INFO] Found {len(games_to_post)} games in 1-hour window")

    if not games_to_post:
        save_state(state)
        return

    pitcher_map = build_pitcher_map()

    for game in games_to_post:
        game_pk = game.get("gamePk")
        try:
            detail = get_json(f"/api/game/{game_pk}")
            hitters = detail.get("hitters", [])
            if not hitters:
                print(f"[WARN] No hitters for game {game_pk}")
                continue

            embed = make_game_embed(game, hitters, pitcher_map)
            post_discord(embeds=[embed])
            mark_posted(state, game_pk)
            save_state(state)
            print(f"[INFO] Posted game {game_pk}")
        except Exception as e:
            print(f"[WARN] Failed game {game_pk}: {e}")

if __name__ == "__main__":
    main()
