# Discord Linux Dogfood

This runbook deploys HealthMes with Hermes Discord delivery without storing
host addresses, Discord IDs, or secrets in git.

## Local-only inputs

Keep these values in the deployment host's untracked `.env`:

```dotenv
HEALTHMES_DELIVERY_PLATFORM=discord
DISCORD_BOT_TOKEN=
DISCORD_ALLOWED_USERS=
DISCORD_HOME_CHANNEL=
DISCORD_HOME_CHANNEL_NAME=HealthMes
ANTHROPIC_API_KEY=
HEALTHMES_API_TOKEN=
OPEN_WEARABLES_API_KEY=
HEALTHMES_TIMEZONE=
```

`DISCORD_ALLOWED_USERS` must contain explicit numeric user IDs. Wildcard
access is rejected by the HealthMes bootstrap.

The Discord application must enable Message Content Intent and the bot must
have permission to view and send messages in `DISCORD_HOME_CHANNEL`.

## Deploy

Run these commands on the Linux deployment host from an untracked clone:

```bash
git fetch origin
git switch codex/discord-linux-dogfood-20260817
cp .env.example .env
# Edit .env locally. Never commit it.
mkdir -p data/hermes
docker compose build healthmes
docker compose run --rm --no-deps \
  -v "$PWD/.env:/srv/healthmes/.env" \
  -v "$PWD/data/hermes:/srv/healthmes/data/hermes" \
  healthmes \
  python scripts/bootstrap.py --mode docker
docker compose up -d --build
```

## Verify

```bash
docker compose ps
curl --fail --silent \
  -H "Authorization: Bearer ${HEALTHMES_API_TOKEN}" \
  http://127.0.0.1:${HEALTHMES_PORT:-8100}/health
docker compose logs --tail=200 hermes
```

Verify a direct Discord conversation, one signed HealthMes webhook alert, and
one temporary cron delivery before relying on the normal briefing schedule.
Then restart the host or Docker daemon and confirm the services and Discord
bot reconnect automatically.

## Known boundary

Hermes Discord supports chat and delivery, but the vendored trusted-session
proof currently accepts Telegram sessions only. Calendar and schedule
confirmation replies therefore remain Telegram-only until that Hermes
contract is generalized in a separate upstream change.
