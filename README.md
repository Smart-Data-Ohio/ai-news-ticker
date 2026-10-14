# AI News Ticker for Slack

A small Python service that polls RSS/Atom feeds, filters for likely AI model releases and major AI stories, and posts a compact digest to a Slack channel. It uses only the Python standard library and stores deduplication state in SQLite.

## How it works

1. Poll configured feeds every 15 minutes (or at the interval in `config.toml` when using `--watch`).
2. Score stories using release, model, and major-news terms. Adjust the source list and threshold in `config.toml` and the phrase lists in `bot.py`.
3. Keep qualifying stories in a local SQLite queue. URLs are the deduplication key.
4. Send up to five queued stories as a single Slack message. A failed Slack request leaves them queued for the next run.

This first version uses each feed's headline and excerpt; it does not invent summaries or claim to verify a story. The feed mix includes primary lab sources plus TechCrunch AI and a Google News query for labs without a stable official RSS feed. X timelines for OpenAI, Anthropic, Google DeepMind, Meta AI, xAI, and Mistral AI are read through RSSHub's public community relay; no X API token is needed, but the relay may be rate-limited or temporarily unavailable. Add or remove feeds in `config.toml` as needed.

## Set up Slack

1. In Slack API, create an app from `slack-app-manifest.yaml` (or create a blank app and add the bot scope `chat:write`).
2. Install the app in your workspace and copy its **Bot User OAuth Token** (`xoxb-…`).
3. Invite the bot to the destination channel. Find the channel ID from the channel details or URL.
4. Copy `.env.example` to `.env`, then fill in the bot token and channel ID. Keep `.env` private.

The bot needs only `chat:write` when the channel ID is known. It must be a member of the channel before posting.

## Run it

Requires Python 3.11 or newer.

```sh
cd ai-news-ticker
cp .env.example .env
# Edit .env with the Slack token and channel ID.
set -a; . ./.env; set +a
```

On first setup, record the current feed contents as already seen so the first live post only contains newer items:

```sh
python3 bot.py --bootstrap
```

Preview later stories without posting:

```sh
python3 bot.py --dry-run
```

Post once:

```sh
python3 bot.py
```

Run continuously in a foreground process:

```sh
python3 bot.py --watch
```

For a persistent deployment, run `python3 /absolute/path/ai-news-ticker/bot.py` from cron every 15 minutes, or use a small always-on host with a process manager and `--watch`. Keep `state.sqlite3` on persistent storage and set `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` as environment variables. To use another database or config path, set `AI_NEWS_DB` or `AI_NEWS_CONFIG`.

## GitHub Actions deployment

The included `.github/workflows/ai-news-ticker.yml` runs every 15 minutes and can also be started manually. It restores the SQLite state from the Actions cache and saves it after each run so scheduled runs can deduplicate stories.

In the repository's **Settings → Secrets and variables → Actions**, add:

- Repository secret `SLACK_BOT_TOKEN` with the Slack bot token (`xoxb-…`).
- Repository variable `SLACK_CHANNEL_ID` with the destination channel ID.

The first scheduled run automatically records current feed items as already seen and does not post them. Subsequent runs post only newer qualifying stories. You can also manually run the workflow with `bootstrap` checked to reset the baseline. The cache is convenient state storage for this lightweight bot; it is not a substitute for a separately backed-up database.

## Tuning

- Increase `minimum_score` to reduce noise; lower it to catch more stories.
- `max_age_hours` avoids posting stale RSS backlog.
- `max_items_per_post` caps digest size; older queued items remain for future runs.
- Add feed entries as `[[feeds]]` with `name`, `url`, and optional `ai_focused = true`.
- Add terms to `RELEASE_TERMS`, `MAJOR_TERMS`, and `AI_TERMS` in `bot.py` to tune ranking.

## Operational notes

- If a feed is temporarily unavailable, the poll continues with the remaining sources and logs the failure to stderr.
- The SQLite file is the durable queue and deduplication store. Back it up if the service is important.
- Slack posting uses `chat.postMessage` and checks Slack's `ok` response before marking stories sent.
- Run only one poller against the same database at a time.