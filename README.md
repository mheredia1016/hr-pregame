# 1-Hour Pregame HR Discord Alert

Run this every 15 minutes. It only posts games starting around 1 hour from now and skips games already posted.

## Railway Variables

```bash
HR_API_BASE=https://hr-api-production-fed2.up.railway.app
HR_PREGAME_WEBHOOK_URL=https://discord.com/api/webhooks/XXXXX/YYYYY
PREGAME_WINDOW_MINUTES=60
PREGAME_GRACE_MINUTES=20
MIN_HR_SCORE=45
TOP_PER_TEAM=3
```

## Railway Cron

Run every 15 minutes:

```cron
*/15 * * * *
```

Start command:

```bash
python hr_pregame_one_hour_alert.py
```

## Output

Each alert includes:
- top 3 HR targets per team
- hitter kHR, xwOBAcon, ISO, HH%, SwStr%
- opposing pitcher HH allowed, FB%, Brl/BIP%, xwOBA
- possible 2-man HR parlay
