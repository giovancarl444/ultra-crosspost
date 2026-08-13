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
| 1 | Telegram core — allow-listed chat, approval buttons | next |
| 2 | Drive queue + SQLite state | |
| 3 | Discord publishing | |
| 4 | Reddit publishing | |
| 5 | Hardening — RedGifs video path, Retry, restart reconciliation, Docker | |

X is **not** in the roadmap — see [X (deferred)](#x-deferred).

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

- **Video goes to RedGifs first.** Reddit and Discord both render a `redgifs.com` link as
  an inline player, so a link post is a full-quality post — not a degraded one — and it
  sidesteps Discord's 10 MiB attachment ceiling entirely. The upload flow is four calls
  over plain `httpx` (`/v1/oauth/weblogin` → `/v1/gifs/submit` → `PUT` the bytes →
  poll `/v1/gifs/fetch/status/{id}`), so it needs no extra dependency.
- **A file over 10 MB can't be previewed as a photo in Telegram**, and one over 50 MB
  can't be uploaded at all; those are sent for approval as filename + Drive link instead.
- **Media DM'd to the bot is capped at 20 MB** — that is a download limit on Telegram's
  side, so the bot physically cannot fetch a larger file. Those are rejected with a note.
- **Captions over 1024 characters** can't ride along with the preview media, so the
  preview sends the media first and the full text as a follow-up message.

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
- No `tweepy` (X is deferred) and no `redgifs` package — the PyPI one is read-only and has
  no upload support, so RedGifs is called directly over `httpx`.

## Layout

```
app/
  __main__.py        entrypoint, --check-config
  config.py          .env + profiles.yaml loading and validation
  models.py          Item, ItemStatus, the caption convention
  logging_setup.py   logging + secret redaction
  platforms/
    base.py          the publisher interface: item + caption in, result out
```

`platforms/` is deliberately free of any Telegram or Drive concept, so a future web UI
can drive the same adapters.
