# Crosspost Engine

Human-approved multi-platform crossposting. One self-hosted asyncio process watches a
Google Drive folder per brand, sends each new file to a private Telegram chat for
approval, and — only after an explicit 🚀 Post tap — publishes to that brand's Reddit
and Discord, then files the Drive file away.

Nothing is ever published without a deliberate tap.

```
Drive inbox/ ──▶ queue ──▶ Telegram: ✅ Approve / ❌ Decline / ⏭ Later
                              │
                              ├─ Approve ─▶ "reply with the post text"
                              │              first line = Reddit title
                              │              rest      = caption + Reddit body
                              │
                              └─▶ preview ─▶ 🚀 Post / ✏️ Edit / ✖️ Cancel
                                               │
                                               ├─▶ Reddit  ─┐
                                               └─▶ Discord ─┴─▶ per-platform ✅/❌ + links
                                                                 (Retry re-runs only failures)
                                                                        │
                                          Drive file ──▶ posted/ or rejected/  (never deleted)
```

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Scaffold, config, README | **done** |
| 1 | Telegram core — allow-listed chat, approval buttons | **done** |
| 2 | Drive queue + SQLite state | next |
| 3 | Discord publishing | |
| 4 | Reddit publishing — native video, link passthrough | |
| 5 | Hardening — Retry, restart reconciliation, Docker | |

X is **not** in the roadmap — see [X (deferred)](#x-deferred).

## Running it

```bash
.venv/bin/python -m app                 # starts the bot
.venv/bin/python -m app --check-config   # validates without touching the network
```

In an allow-listed chat the bot answers two commands:

| Command | Effect |
| --- | --- |
| `/start` | Confirms which profile the chat is bound to and echoes the chat id. |
| `/test` | Sends `assets/test.png` as an approval card, to check the buttons work. |

Every update from any other chat is ignored — but its chat id is logged at INFO, which is
how you onboard a new chat without loosening the allow-list.

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env          # then fill it in
.venv/bin/python -m app --check-config
```

`--check-config` parses `profiles.yaml`, prints every profile, and reports which
credentials are still missing **by environment-variable name**. It never prints a secret
value and it makes no network calls, so it is safe to run at any point.

Exit codes: `0` complete · `1` valid but credentials missing · `2` config invalid.

## Configuration

Two files, with a firm split:

- **`profiles.yaml`** — everything that is not a secret. Committed. It refers to
  credentials only by the *name* of the environment variable holding them (`webhook_env:
  DISCORD_WEBHOOK_BRAND_A`), so adding a brand never means touching code.
- **`.env`** — every secret. Gitignored. Start from `.env.example`.

### Profile keys

| Key | Meaning |
| --- | --- |
| `name` | Profile id. Must be unique. |
| `telegram_chat_id` | The only chat this profile responds in. Must be unique. |
| `drive.inbox/posted/rejected_folder_id` | Three distinct folders you create and share with the service account. |
| `reddit.subreddit` | Target sub, with or without the `r/` prefix. |
| `reddit.nsfw` | Marks submissions NSFW. Required for NSFW subs — unflagged posts get removed. |
| `reddit.flair_text` / `flair_id` | Post flair. Many subs reject unflaired posts. |
| `redgifs.*` | Media host for video (see below). Not a posting target. |
| `<platform>.enabled` | Per-platform on/off, per profile. |

`DRY_RUN=true` (the default) runs the entire flow and simulates the platform calls,
logging what would have been posted and returning fake URLs.

## Credentials

Obtained per phase, so you only ever chase what the next step needs.

### Telegram bot token and chat id — needed for Phase 1

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, give it a display name, then a username ending in `bot`.
3. BotFather replies with a token like `8123456789:AAF...`. Put it in `.env` as
   `TELEGRAM_BOT_TOKEN=`. Treat it like a password — anyone holding it controls the bot.
4. Send `/setprivacy` → pick your bot → **Disable**, so it can see media you post in a
   group. (Skip this if you will only ever DM the bot directly.)
5. Now find your chat id. Send any message to your new bot first — a bot cannot start a
   conversation — then:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
   ```
   The number is your chat id. It is **negative** for groups and channels.
6. Put that number in `profiles.yaml` as `telegram_chat_id`. Any update from any other
   chat is silently ignored.

### Google Drive service account — Phase 2
### Discord webhook — Phase 3
### Reddit script app — Phase 4
### RedGifs account — Phase 5

Step-by-step guides land here as each phase is built.

## Platform reality

Checked against primary documentation on 2026-08-13 rather than from memory, because
several of these changed recently.

| Limit | Value |
| --- | --- |
| Telegram bot **upload** | 10 MB photos, 50 MB other files |
| Telegram bot **download** (`getFile`) | **20 MB** |
| Telegram caption | 1024 characters |
| Discord webhook attachment | 10 MiB default, higher only on boosted servers |
| Reddit API | free for non-commercial use, 100 requests/min per OAuth client |

Consequences baked into the design:

- **A file over 10 MB can't be previewed as a photo in Telegram**, and one over 50 MB
  can't be uploaded at all; those are sent for approval as filename + Drive link instead.
- **Media DM'd to the bot is capped at 20 MB** — that is a download limit on Telegram's
  side, so the bot physically cannot fetch a larger file. Those are rejected with a note.
- **Captions over 1024 characters** can't ride along with the preview media, so the
  preview sends the media first and the full text as a follow-up message.

### Video, and why RedGifs is not integrated

RedGifs would have been the natural video host — Reddit renders a `redgifs.com` link as an
inline player. It is not integrated because the API is no longer scriptable. Probing the
live service rather than trusting its published spec:

| Endpoint in the published 1.0.0 spec | Live response |
| --- | --- |
| `POST /v1/oauth/weblogin` | `404 HttpNotFoundException` — gone |
| `POST /v2/auth/login` | `400` — *"missing field `captcha`"* |
| `POST /v2/oauth/token` | `401 UnsupportedGrant` — *"must be: authorization_code, or refresh_token"*, and needs a registered `client_id` |
| `GET /v2/auth/temporary` | works, but the token carries `scopes: read` |

Password login is captcha-gated and token exchange needs an OAuth client that RedGifs no
longer issues on request. Automating around that would mean driving a browser session on a
brand account — fragile, and against their terms.

What the engine does instead:

- **Reddit takes video natively.** `Subreddit.submit(video=...)` produces a `v.redd.it`
  post with Reddit's own inline player — the same result a RedGifs embed gives.
- **A URL in the caption becomes a link post.** If the caption body contains a URL, Reddit
  posts *that link* with the remaining text as the Markdown body. So the manual RedGifs
  workflow still works end to end: upload by hand, paste the link into the caption, and
  the bot publishes it as a properly embedded link post.
- **Discord** attaches the file under the 10 MiB webhook ceiling and otherwise posts the
  caption plus whichever URL the item carries, reported as `DEGRADED` rather than a clean ✅.

### X (deferred)

X is not implemented. Two things make it a decision rather than a task:

1. **There is no free tier.** X discontinued it in February 2026 and moved to
   pay-per-usage credits ($5 minimum purchase):

   | Action | Cost |
   | --- | --- |
   | Post: Create | $0.015 per request |
   | Post: Create **containing a URL** | **$0.200 per request** |

   The URL surcharge bites here specifically: a RedGifs-link post to X costs $0.20, so
   100 video posts a month is ~$20 rather than ~$1.50. The cheap third-party "X API"
   providers are read/scraping services; the few exposing a post endpoint drive
   unofficial user sessions, which risks the account.

2. **`tweepy` can no longer upload media.** X deprecated the v1.1 media endpoints in June
   2025, and tweepy 4.17.0 still implements only those — its v2 `Client` has no media
   methods at all.

If you want X later, the build is: `httpx` + `oauthlib` against `POST /2/tweets` and the
`/2/media/upload` INIT/APPEND/FINALIZE/STATUS flow. OAuth 1.0a user context is accepted
on both, so ordinary consumer key/secret + access token/secret signing is enough — no
PKCE flow needed. The `x:` block already exists in `profiles.yaml`; today setting
`enabled: true` is a deliberate config error rather than a silent no-op.

## Dependency notes

Pinned in `requirements.txt` to the versions current on 2026-08-13.

- **`asyncpraw` 8.x is a breaking change** from 7.x and from essentially every tutorial
  online: `submit_image` / `submit_video` / `submit_gallery` / `submit_poll` were merged
  into `Subreddit.submit()`, selected by the `image=` / `video=` / `gallery=` / `poll=`
  keyword, and media is passed as `PostMedia` objects rather than file paths. It also
  gained Markdown bodies on media posts — which is what makes the "first line is the
  title, the rest is the body" convention work on Reddit image and video posts at all.
- **`PyYAML`** and **`python-dotenv`** are not named in the original spec but are implied
  by its own choice of `profiles.yaml` + `.env`. `python-dotenv` is droppable if you would
  rather inject environment variables purely through Docker Compose.
- `google-api-python-client` is synchronous; Drive calls are made via `asyncio.to_thread`
  so they never block the event loop.
- No `tweepy` (X is deferred) and no `redgifs` (not integrated — see above).

## Layout

```
app/
  __main__.py        entrypoint, --check-config
  service.py         composition root: one event loop, ordered shutdown
  config.py          .env + profiles.yaml loading and validation
  models.py          Item, ItemStatus, the caption convention
  logging_setup.py   logging + secret redaction
  telegram/
    bot.py           Application construction and the chat allow-list
    handlers.py      commands, approval keyboard, size-aware send helper
  platforms/
    base.py          the publisher interface: item + caption in, result out
assets/test.png      approval card used by /test
```

`platforms/` is deliberately free of any Telegram or Drive concept, so a future web UI
can drive the same adapters.
