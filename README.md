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
| 2 | Drive queue + SQLite state | **done** |
| 3 | Discord publishing — caption, preview, post, retry | **done** |
| 4 | Reddit publishing — native video, link passthrough | next |
| 5 | Hardening — restart reconciliation, Docker | |

X is **not** in the roadmap — see [X (deferred)](#x-deferred).

## Running it

```bash
.venv/bin/python -m app                 # starts the bot
.venv/bin/python -m app --check-config   # validates without touching the network
```

In an allow-listed chat the bot answers two commands:

| Command | Effect |
| --- | --- |
| `/start` | Names the profile, reports whether Drive is connected, and shows the queue. |
| `/test` | Queues the bundled `assets/test.png` and sends it as an approval card. |
| *send a photo or video* | Queues it exactly like a Drive file — see below. |

Every update from any other chat is ignored — but its chat id is logged at INFO, which is
how you onboard a new chat without loosening the allow-list.

Without a service-account file the bot still runs; it logs that Drive polling is off and
media sent directly to it still queues. That makes the whole approval loop testable before
any Google setup exists.

### The queue

A file in a `inbox` folder becomes an item in SQLite, is downloaded to `media/`, and is
sent to the profile's chat as an approval card:

- **✅ Approve** — moves to `awaiting_text` (the caption step is Phase 3).
- **❌ Decline** — the Drive file is moved to `rejected/` and the local copy deleted.
  Nothing is ever hard-deleted.
- **⏭ Later** — back to the queue, re-offered after `later_cooldown_minutes` (default 60)
  so the next poll does not immediately show the same card again.

Media sent straight to the bot joins the same queue. It has no Drive file behind it, so it
is never archive-moved. Telegram caps what a bot may **download** at 20 MB — well under
what it may send — so anything larger is refused with an explanation rather than failing
silently; put those in the Drive inbox instead.

### Approve → caption → preview → post

Approve starts a short conversation:

1. The bot asks for the post text. **First line is the Reddit title; everything after it is
   the caption** used for the Discord message and the Reddit body.
2. It replies with a preview — what goes where, plus any warning worth knowing *before*
   publishing (caption over Discord's 2000 characters, file over the 10 MiB webhook limit,
   no platforms enabled) — and three buttons: **🚀 Post**, **✏️ Edit text**, **✖️ Cancel**.
3. Only 🚀 Post publishes. Platforms run concurrently, and the result message reports each
   one with ✅ posted / ⚠️ posted but degraded / ❌ failed, with links.
4. If anything failed, a **🔁 Retry failed** button re-attempts *only* the platforms that
   did not succeed. A platform that already posted is never posted to again — that is
   enforced by the `post_results` table, so it survives a restart too.
5. When every enabled platform has succeeded, the Drive file moves to `posted/`.

Only one item per profile can be between Approve and Post at a time. Approving a second
one says so rather than accepting it, because otherwise a reply carrying the post text
would be ambiguous about which item it belonged to.

Offers are capped at `max_offers_per_cycle` (default 5) per poll, because Telegram
rate-limits bulk sends to one chat. Dropping 40 files into the inbox trickles them out
over cycles rather than getting throttled.

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env                      # secrets
cp profiles.example.yaml profiles.yaml    # brands, chats, Drive folders
.venv/bin/python -m app --check-config
```

`--check-config` parses `profiles.yaml`, prints every profile, and reports which
credentials are still missing **by environment-variable name**. It never prints a secret
value and it makes no network calls, so it is safe to run at any point.

Exit codes: `0` complete · `1` valid but credentials missing · `2` config invalid.

## Running it on Windows with Docker Desktop

The engine is a daemon — it has to stay running to poll Drive. These steps need no
terminal; the `.bat` files are double-clickable.

**1. Install Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/)
and launch it. Wait until the bottom-left says **Engine running**. In its Settings, tick
**Start Docker Desktop when you log in** so the engine comes back after a reboot.

**2. Get the code.** On the GitHub repo page: green **Code** button → **Download ZIP** →
extract it somewhere permanent, e.g. `C:\crosspost-engine`. (No git required.)

**3. Put your three config files in that folder**, next to `docker-compose.yml`:

| File | From |
| --- | --- |
| `.env` | copy `.env.example`, fill in the secrets |
| `profiles.yaml` | copy `profiles.example.yaml`, fill in chats and folder ids |
| `service-account.json` | the key you downloaded from Google Cloud |

To copy a file in Explorer: right-click → Copy, right-click → Paste, then rename. Make sure
Explorer is showing file extensions (View → Show → File name extensions) so you don't end
up with `.env.txt`.

**4. Double-click `windows\check-config.bat`** — it prints your profiles and lists any
missing credentials by name. It never prints a secret value.

**5. Double-click `windows\start.bat`.** The first run builds the image and takes a few
minutes. After that it starts in seconds and keeps running in the background.

| File | What it does |
| --- | --- |
| `windows\start.bat` | Starts the engine. Safe to run again; it just restarts. |
| `windows\logs.bat` | Live log. Closing the window does **not** stop the engine. |
| `windows\stop.bat` | Stops it. The queue and downloaded media are kept. |
| `windows\check-config.bat` | Validates config without starting anything. |

`restart: unless-stopped` means the engine survives a reboot and restarts itself if it ever
crashes. Your queue lives in `data\crosspost.db` and downloaded media in `data\media\`,
both outside the container, so rebuilding never loses state.

**The catch with this setup:** it only runs while your PC is on and online. Approvals you
tap while it is asleep will not go anywhere until it wakes. If that becomes annoying, the
same `docker compose up -d` runs unchanged on any always-on Linux host.

### Rotating credentials

Anything pasted into a chat should be considered burned. Before real use:

- Telegram: `/revoke` in BotFather, then put the new token in `.env`.
- Google: Cloud Console → Service Accounts → your account → **Keys** → delete the old key,
  **Add key** → JSON, and replace `service-account.json`.
- Discord: Server Settings → Integrations → Webhooks → delete and recreate, update `.env`.

Note that `docker compose config` prints your environment in full, secrets included — handy
for debugging, but don't paste its output anywhere.

## Configuration

Two files, with a firm split:

- **`profiles.yaml`** — brands, chat ids, Drive folder ids. Gitignored, because those
  identify your accounts; `profiles.example.yaml` is the committed template. It refers to
  credentials only by the *name* of the environment variable holding them (`webhook_env:
  DISCORD_WEBHOOK_BRAND_A`), so it never contains a secret and adding a brand never means
  touching code.
- **`.env`** — every secret. Gitignored. Start from `.env.example`.

A profile with no platforms enabled is allowed: it queues, shows approval cards and
archives, but publishes nothing. That is the normal state while platforms are still being
added, and `--check-config` labels it rather than failing.

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

### Keeping credentials out of git

`scripts/scan-secrets.sh` refuses to commit anything matching a bot token, Discord webhook,
private key, service-account JSON or AWS key, and refuses `.env`, `service-account*.json`
and `profiles.yaml` whatever their contents. Install it as a hook once per clone:

```bash
git config core.hooksPath scripts/githooks
```

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

### Google Drive service account — needed for Phase 2

A service account is a robot Google account with its own email address. You share your
folders with it exactly as you would with a colleague; it can only ever see what you share.

**1. Make a project and turn on the API**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in with the
   Google account that owns the Drive folders.
2. Project dropdown (top bar) → **New project** → name it e.g. `crosspost-engine` →
   **Create**, then make sure it is selected.
3. Search bar → **Google Drive API** → **Enable**. Nothing works without this step.

**2. Create the service account and its key**

4. Navigation menu → **IAM & Admin** → **Service Accounts** → **Create service account**.
5. Name it e.g. `crosspost-bot` → **Create and continue** → skip the optional role and
   user-access steps → **Done**. You do *not* need to grant it any project role; all its
   power comes from Drive sharing.
6. Click the new account → **Keys** tab → **Add key** → **Create new key** → **JSON** →
   **Create**. A `.json` file downloads. **This is a credential — treat it like a password.**
7. Put that file in the project folder as `service-account.json` (it is gitignored), and
   point `GOOGLE_SERVICE_ACCOUNT_FILE` at it in `.env`.

**3. Share your folders with it**

8. Open the downloaded JSON and copy the `"client_email"` value. It looks like
   `crosspost-bot@your-project.iam.gserviceaccount.com`.
9. In Google Drive, create a parent folder for the brand with three subfolders inside it:
   `inbox`, `posted`, `rejected`. The app never creates or deletes folders, so these must
   exist first.
10. Right-click the **parent** folder → **Share** → paste the `client_email` → set it to
    **Editor** → uncheck "Notify people" → **Share**. Editor is required because archiving
    moves a file between folders.

**4. Get the three folder ids**

11. Open each subfolder in Drive and read the id out of the address bar — it is the part
    after `/folders/`:
    `https://drive.google.com/drive/folders/`**`1a2B3cD4eF5gH6iJ7kL8mN9oP`**
12. Put those three ids into `profiles.yaml` under `drive.inbox_folder_id`,
    `posted_folder_id` and `rejected_folder_id`.

**5. Check it**

`python -m app` logs the service-account address on startup, and any folder it cannot
reach is reported rather than silently skipped. If files never appear, the usual cause is
step 10 — the folder was not shared, or was shared as Viewer instead of Editor.

### Discord webhook — needed for Phase 3

1. In Discord, open **Server Settings** → **Integrations** → **Webhooks** → **New Webhook**.
2. Pick the channel it should post into, give it a name, then **Copy Webhook URL**.
3. Put that URL in `.env` under the variable named by the profile's `webhook_env`
   (e.g. `DISCORD_WEBHOOK_APD=`). **The URL is the credential** — anyone holding it can post
   to that channel, so it never goes in `profiles.yaml`.

No bot invite or OAuth is involved; a webhook posts on its own. Uploads are capped at
10 MiB unless the server is boosted, so larger video posts the caption and link instead and
is reported as ⚠️ rather than ✅.

### Reddit script app — Phase 4

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
  db.py              SQLite state: the queue and per-platform results
  drive.py           Google Drive v3 via a service account (list/download/move)
  intake.py          getting media into the queue and archiving it out again
  poller.py          the Drive poll loop
  logging_setup.py   logging + secret redaction
  publish.py         fan an approved item out to every platform, concurrently
  telegram/
    bot.py           Application construction and the chat allow-list
    handlers.py      commands, the approval conversation, media sent to the bot
    cards.py         message composition — no database, no network
  platforms/
    base.py          the publisher interface: item + caption in, result out
    discord.py       webhook publishing
assets/test.png      approval card used by /test
scripts/
  scan-secrets.sh    refuses to commit anything that looks like a credential
```

`platforms/` is deliberately free of any Telegram or Drive concept, so a future web UI
can drive the same adapters.
