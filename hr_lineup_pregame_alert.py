import json, os, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

API_BASE = os.getenv("HR_API_BASE", "https://hr-api-production-fed2.up.railway.app").rstrip("/")
WEBHOOK_URL = os.getenv("HR_PREGAME_WEBHOOK_URL", "").strip()
TZ = ZoneInfo("America/Chicago")

ALLOW_UNCONFIRMED_LINEUPS = os.getenv("ALLOW_UNCONFIRMED_LINEUPS", "false").lower() == "true"
STATE_FILE = Path(os.getenv("PREGAME_STATE_FILE", "/tmp/hr_lineup_pregame_posted_games.json"))
TOP_PER_TEAM = int(os.getenv("TOP_PER_TEAM", "3"))
MIN_HR_SCORE = float(os.getenv("MIN_HR_SCORE", "45"))

def safe_float(v, default=0.0):
    try:
        if v is None or v == "" or v == "—": return default
        return float(str(v).replace("%","").replace(",",""))
    except Exception:
        return default

def fmt_num(v, digits=1):
    if v is None: return "—"
    try: return f"{float(v):.{digits}f}"
    except Exception: return str(v)

def parse_game_time(game_date):
    if not game_date: return None
    try: return datetime.fromisoformat(game_date.replace("Z","+00:00")).astimezone(TZ)
    except Exception: return None

def fmt_game_time(game_date):
    dt = parse_game_time(game_date)
    return dt.strftime("%-I:%M %p CT") if dt else "Time TBD"

def get_json_url(url):
    r = requests.get(url, timeout=45)
    r.raise_for_status()
    return r.json()

def get_json(path):
    return get_json_url(f"{API_BASE}{path}")

def post_discord(content=None, embeds=None):
    if not WEBHOOK_URL:
        raise RuntimeError("Missing HR_PREGAME_WEBHOOK_URL")
    payload = {}
    if content: payload["content"] = content
    if embeds: payload["embeds"] = embeds
    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()

def load_state():
    try:
        if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    except Exception: pass
    return {"posted":{}}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))

def cleanup_state(state):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    state["posted"] = {k:v for k,v in state.get("posted",{}).items() if str(v).startswith(today)}
    return state

def already_posted(state, game_pk):
    return str(game_pk) in state.get("posted",{})

def mark_posted(state, game_pk):
    state.setdefault("posted",{})[str(game_pk)] = datetime.now(TZ).isoformat()

def player_id_int(x):
    try: return int(x)
    except Exception: return None

def get_official_lineups(game_pk):
    try:
        data = get_json_url(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live")
        box = (((data.get("liveData") or {}).get("boxscore") or {}).get("teams") or {})
        out = {"confirmed":False, "away":{}, "home":{}}
        for side in ["away","home"]:
            team = box.get(side) or {}
            for idx, pid_raw in enumerate(team.get("battingOrder") or []):
                pid = player_id_int(pid_raw)
                if pid: out[side][pid] = idx + 1
            for key, pdata in (team.get("players") or {}).items():
                person = pdata.get("person") or {}
                pid = player_id_int(person.get("id") or str(key).replace("ID",""))
                bo = pdata.get("battingOrder")
                if pid and bo:
                    try: out[side][pid] = int(str(bo)[:1])
                    except Exception: pass
        out["confirmed"] = len(out["away"]) >= 8 and len(out["home"]) >= 8
        return out
    except Exception as e:
        print(f"[WARN] Could not read official lineup for {game_pk}: {e}")
        return {"confirmed":False, "away":{}, "home":{}}

def build_pitcher_map():
    try:
        rows = get_json("/api/pitchers").get("pitchers", [])
        return {str(r.get("pitcher") or "").strip().lower(): r for r in rows if r.get("pitcher")}
    except Exception as e:
        print(f"[WARN] Could not load pitcher report: {e}")
        return {}

def lineup_boost(spot):
    if not spot: return -3.0
    if spot in [2,3,4]: return 5.0
    if spot in [1,5]: return 3.0
    if spot == 6: return 1.0
    if spot in [7,8,9]: return -2.0
    return 0.0

def hitter_hr_score(h, pitcher=None):
    khr = safe_float(h.get("kHR"))
    xcon = safe_float(h.get("xwOBAcon"))
    iso = safe_float(h.get("ISO"))
    hh = safe_float(h.get("HH"))
    swstr = safe_float(h.get("swStr"))
    p_hh = safe_float((pitcher or {}).get("HH"))
    p_fb = safe_float((pitcher or {}).get("FB"))
    p_brl = safe_float((pitcher or {}).get("brlBip"))
    p_xwoba = safe_float((pitcher or {}).get("xwOBA"))

    score = 0
    score += khr * 0.38
    score += (xcon * 100) * 0.25
    score += (iso * 100) * 0.18
    score += hh * 0.12
    score += max(0, p_hh - 38) * 0.18
    score += max(0, p_fb - 26) * 0.12
    score += max(0, p_brl - 6) * 0.55
    score += max(0, (p_xwoba - .315) * 100) * 0.25
    score += lineup_boost(h.get("lineupSpot"))
    score -= max(0, swstr - 13) * 0.65
    return round(score, 1)

def parlay_fit(a, b):
    sa, ha, pa = a; sb, hb, pb = b
    fit = (sa + sb) / 2
    if str(ha.get("team")) == str(hb.get("team")) and str(ha.get("pitcher")) == str(hb.get("pitcher")):
        fit += 9
    elif str(ha.get("pitcher")) == str(hb.get("pitcher")):
        fit += 6
    spots = [ha.get("lineupSpot"), hb.get("lineupSpot")]
    if all(s in [2,3,4,5] for s in spots if s): fit += 6
    elif all(s in [1,2,3,4,5,6] for s in spots if s): fit += 3
    p = pa or pb or {}
    fit += max(0, safe_float(p.get("HH")) - 40) * .15
    fit += max(0, safe_float(p.get("FB")) - 28) * .10
    fit += max(0, safe_float(p.get("brlBip")) - 7) * .45
    sw = safe_float(ha.get("swStr")) + safe_float(hb.get("swStr"))
    if sw > 30: fit -= 8
    elif sw > 25: fit -= 4
    return round(max(0, min(100, fit)), 1)

def tag(score):
    return "🔥 Best" if score >= 65 else "💣 Strong" if score >= 56 else "🎯 Live" if score >= 48 else "👀 Lean"

def risk(fit):
    return "Medium" if fit >= 82 else "Medium-High" if fit >= 72 else "High"

def team_rows(rows, team):
    return [r for r in rows if str(r.get("team","")).upper() == str(team).upper()]

def attach_lineup_spots(hitters, lineup_map, side):
    lineup = lineup_map.get(side, {})
    out = []
    for h in hitters:
        pid = player_id_int(h.get("playerId"))
        if pid in lineup:
            out.append({**h, "lineupSpot": lineup[pid]})
    return out

def sort_hitters(rows, pitcher_map):
    scored = []
    for h in rows:
        p = pitcher_map.get(str(h.get("pitcher","")).strip().lower())
        scored.append((hitter_hr_score(h, p), h, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored

def line_for_hitter(rank, score, h, p):
    pitcher = h.get("pitcher") or "TBD"
    spot = f"#{h.get('lineupSpot')} " if h.get("lineupSpot") else ""
    if p:
        p_bits = f"\n   vs {pitcher}: HH allowed {fmt_num(p.get('HH'))}% | FB {fmt_num(p.get('FB'))}% | Brl/BIP {fmt_num(p.get('brlBip'))}% | xwOBA {fmt_num(p.get('xwOBA'),3)}"
    else:
        p_bits = f"\n   vs {pitcher}: pitcher risk unavailable"
    return (
        f"**{rank}. {spot}{h.get('name','Unknown')}** — {tag(score)} `{score}`\n"
        f"   kHR {fmt_num(h.get('kHR'))} | xwOBAcon {fmt_num(h.get('xwOBAcon'),3)} | ISO {fmt_num(h.get('ISO'),3)} | HH {fmt_num(h.get('HH'))}% | SwStr {fmt_num(h.get('swStr'))}%"
        f"{p_bits}"
    )

def best_correlated_two_man(a_rows, h_rows):
    candidates = []
    for pool in [a_rows[:5], h_rows[:5]]:
        for i in range(len(pool)):
            for j in range(i+1, len(pool)):
                fit = parlay_fit(pool[i], pool[j])
                candidates.append((fit, pool[i], pool[j]))
    merged = a_rows[:3] + h_rows[:3]
    for i in range(len(merged)):
        for j in range(i+1, len(merged)):
            candidates.append((parlay_fit(merged[i], merged[j]) - 5, merged[i], merged[j]))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0] if candidates and candidates[0][0] >= MIN_HR_SCORE else None

def best_team_stack(team_scored):
    top = team_scored[:TOP_PER_TEAM]
    if len(top) < 2:
        return None, None

    candidates = []
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            a, b = top[i], top[j]
            fit = parlay_fit(a, b)
            candidates.append((fit, a, b))

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0] if candidates else None
    alt = candidates[1] if len(candidates) > 1 else None
    return best, alt

def stack_text(title, stack):
    if not stack:
        return ""
    fit, a, b = stack
    reasons = parlay_reasons(a, b)
    text = (
        f"\n\n{title}\n"
        f"{a[1].get('name')} + {b[1].get('name')}\n"
        f"Stack Score: `{fit}/100` | Risk: **{risk(fit)}**"
    )
    if reasons:
        text += "\nWhy: " + " • ".join(reasons)
    return text


def parlay_reasons(a, b):
    _, ha, pa = a; _, hb, pb = b
    reasons = []
    if str(ha.get("pitcher")) == str(hb.get("pitcher")): reasons.append("same weak pitcher")
    if ha.get("lineupSpot") and hb.get("lineupSpot"): reasons.append(f"lineup spots #{ha.get('lineupSpot')} + #{hb.get('lineupSpot')}")
    p = pa or pb
    if p:
        if safe_float(p.get("HH")) >= 40: reasons.append("pitcher allows hard contact")
        if safe_float(p.get("FB")) >= 28: reasons.append("pitcher allows fly-ball damage")
        if safe_float(p.get("brlBip")) >= 7: reasons.append("pitcher barrel risk")
    if (safe_float(ha.get("xwOBAcon")) + safe_float(hb.get("xwOBAcon"))) / 2 >= .380: reasons.append("strong xwOBAcon combo")
    if safe_float(ha.get("swStr")) + safe_float(hb.get("swStr")) <= 25: reasons.append("acceptable combined SwStr")
    return reasons[:5]

def make_game_embed(game, hitters, pitcher_map, lineup_map):
    away = game.get("away", {}).get("abbreviation", "AWAY")
    home = game.get("home", {}).get("abbreviation", "HOME")
    label = game.get("label") or f"{away} @ {home}"

    away_rows = attach_lineup_spots(team_rows(hitters, away), lineup_map, "away")
    home_rows = attach_lineup_spots(team_rows(hitters, home), lineup_map, "home")

    away_scored = sort_hitters(away_rows, pitcher_map)
    home_scored = sort_hitters(home_rows, pitcher_map)

    away_best, away_alt = best_team_stack(away_scored)
    home_best, home_alt = best_team_stack(home_scored)

    desc = f"**{label} — {fmt_game_time(game.get('gameDate'))}**\nLineups confirmed ✅\n\n"

    desc += f"__**{away} Top HR Targets**__\n"
    desc += "\n".join(line_for_hitter(i+1, s, h, p) for i,(s,h,p) in enumerate(away_scored[:TOP_PER_TEAM])) or "No confirmed hitters found."
    desc += stack_text("💰 **Best HR Stack**", away_best)
    desc += stack_text("🎯 **Alternate Stack**", away_alt)

    desc += "\n\n"
    desc += f"__**{home} Top HR Targets**__\n"
    desc += "\n".join(line_for_hitter(i+1, s, h, p) for i,(s,h,p) in enumerate(home_scored[:TOP_PER_TEAM])) or "No confirmed hitters found."
    desc += stack_text("💰 **Best HR Stack**", home_best)
    desc += stack_text("🎯 **Alternate Stack**", home_alt)

    return {
        "title": "🚨 Official Lineup HR Targets",
        "description": desc[:4000],
        "color": 15158332,
        "footer": {"text": "Top 3 per team + best/alternate same-team HR stacks. Score = kHR + xwOBAcon + ISO + pitcher risk + lineup spot - SwStr"}
    }

def main():
    state = cleanup_state(load_state())
    games_data = get_json("/api/games")
    games = games_data.get("games", games_data if isinstance(games_data, list) else [])
    pitcher_map = build_pitcher_map()
    posted = 0

    for game in games:
        game_pk = game.get("gamePk")
        if not game_pk or already_posted(state, game_pk):
            continue
        lineup_map = get_official_lineups(game_pk)
        if not lineup_map.get("confirmed") and not ALLOW_UNCONFIRMED_LINEUPS:
            print(f"[INFO] Game {game_pk}: lineup not confirmed yet. Skipping.")
            continue
        try:
            detail = get_json(f"/api/game/{game_pk}")
            hitters = detail.get("hitters", [])
            if not hitters:
                print(f"[WARN] No hitters for game {game_pk}")
                continue
            post_discord(embeds=[make_game_embed(game, hitters, pitcher_map, lineup_map)])
            mark_posted(state, game_pk)
            save_state(state)
            posted += 1
            print(f"[INFO] Posted official lineup HR alert for game {game_pk}")
        except Exception as e:
            print(f"[WARN] Failed game {game_pk}: {e}")

    if posted == 0:
        print("[INFO] No official lineup alerts to post right now.")
    save_state(state)

if __name__ == "__main__":
    main()