# Official Lineup HR Pregame Alert

Runs every few minutes, but only posts when official MLB lineups are available.

Railway variables:
HR_API_BASE=https://hr-api-production-fed2.up.railway.app
HR_PREGAME_WEBHOOK_URL=your Discord webhook
LINEUP_WINDOW_MINUTES=150
ALLOW_UNCONFIRMED_LINEUPS=false
TOP_PER_TEAM=3
MIN_HR_SCORE=45
ALLOWED_START_HOUR=10
ALLOWED_END_HOUR=23

Cron:
*/5 * * * *

Start command:
python hr_lineup_pregame_alert.py
