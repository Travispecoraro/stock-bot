# Stock Bot

Pings you on Discord whenever someone on your watchlist discloses a stock buy or sell. Runs for free on GitHub Actions every 30 minutes, sends a once-daily heartbeat so you know it's alive, and every feature is toggleable from `config.yaml` — edit, push, done.

Includes a dashboard, ledger, portfolio, & customizable alerts.

## How it works

Every 30 minutes, GitHub Actions runs `monitor.py`, which downloads the full Senate and House disclosure datasets, diffs them against `state.json` (the list of trades it has already seen, committed back to the repo after each run), applies your filters, and posts anything new to your Discord webhook. Buys are green, sells are red, and each alert links to the official disclosure filing.

Congressional trades are disclosed **days to weeks after they happen** (the STOCK Act allows up to 45 days), so a 30-minute polling interval loses you nothing versus a truly continuous process — you're always rate-limited by how slowly Congress files paperwork, not by the bot.

## Setup (one time, ~10 minutes)

**1. Create the Discord webhook.** In your Discord server: Server Settings → Integrations → Webhooks → New Webhook. Pick the channel, copy the webhook URL.

**2. Get your Discord user ID.** Discord Settings → Advanced → enable Developer Mode. Then right-click your own name anywhere → Copy User ID. Paste it into `config.yaml` under `discord.user_id` (keep the quotes).

**3. Create a GitHub repo** (private is fine) and upload all of these files, preserving the `.github/workflows/` folder structure.

**4. Add secrets.** In the repo: Settings → Secrets and variables → Actions → New repository secret:
   - `DISCORD_WEBHOOK_URL` — the URL from step 1
   - `FINNHUB_API_KEY` — your Finnhub key (only used if you enable `finnhub_enrichment`, but add it now so it's there)

**5. Enable and test.** Go to the Actions tab, enable workflows if prompted, click "Congress Trade Monitor" → "Run workflow". The first run indexes all historical trades *silently* (so you don't get flooded with years of backfill) and posts an "initialized" message. Every run after that pings you for new trades only.

## The control panel: `config.yaml`

Everything is toggled here. Edit the file on GitHub (or locally + push) and the next run picks it up automatically — no redeploy.

| Setting | What it does |
|---|---|
| `ping_buys` / `ping_sells` | Turn buy or sell alerts on/off |
| `heartbeat` | Once-daily "I'm alive, found X trades" summary |
| `notify_on_error` | Message you if a data source breaks |
| `senate_source` / `house_source` | Toggle each chamber |
| `finnhub_enrichment` | Extra per-ticker check via Finnhub (needs `watch_tickers`) |
| `min_trade_value` | Skip small trades (uses low end of disclosed range) |
| `watch_tickers` | Empty = all stocks; or limit to a list |
| `watch_politicians` | Empty = everyone; or limit to names (partial match) |
| `lookback_days` | Ignore disclosures older than N days |
| `mention_on_trade` | Whether alerts @you |
| `heartbeat_hour_utc` | When the daily heartbeat fires (14 ≈ 9–10am ET) |
| `max_pings_per_run` | Flood protection cap per run |

## Pausing / stopping

To pause everything without deleting anything: Actions tab → Congress Trade Monitor → "⋯" menu → Disable workflow. Re-enable the same way. (Or set both `ping_buys` and `ping_sells` to `false` to keep the heartbeat running while muting trades.)

Note: GitHub automatically disables scheduled workflows in repos with no commits for 60 days. The bot's own state commits prevent this, but if you ever pause it for months you may need to re-enable it in the Actions tab.

## Data sources & a caveat

Primary sources are the **Senate Stock Watcher** and **House Stock Watcher** public datasets (free, no key, cover all members). These are community-maintained mirrors of the official disclosure sites and have occasionally gone stale in the past — which is exactly why `notify_on_error` exists and why the heartbeat reports source errors. If a source dies permanently, the Finnhub enrichment path (per-ticker, using your API key) is the fallback, and swapping in a different source only means editing one `fetch_*`/`normalize_*` function pair in `monitor.py`.

## Costs

$0. GitHub Actions free tier includes 2,000 minutes/month for private repos (unlimited for public); this bot uses roughly 1 minute per run × 48 runs/day ≈ 1,450 min/month — under the limit, and unlimited if you make the repo public. The data sources and Discord webhooks are free.
