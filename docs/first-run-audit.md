# First-run audit — Crosspost Engine

Produced ahead of the engine's first real run, on a fresh machine, against real
credentials. Split into two parts: what was **verified by execution**, and what an
automated audit **claims but has not had confirmed**.

## Part 1 — verified by execution

These were reproduced by running the code, not by reading it.

### ✅ Working correctly

| Check | Result |
| --- | --- |
| `pip install -r requirements.txt` (Python 3.12) | clean; all imports resolve |
| `python -m app --check-config` (venv) | correct output, exit code 1 as documented |
| Docker image build | clean — COPY paths, non-root uid 10001, assets all correct |
| `--check-config` inside the container | correct; this is what `check-config.bat` runs |
| `assets/test.png` present in image | yes, so `/test` works |
| Double-post guard (`post_results`) | sound — `degraded` counts as posted, upsert does not duplicate, Drive `drive_file_id` dedupe works, connection survives an `IntegrityError` |
| Missing `service-account.json` | degrades gracefully; `is_file()` correctly rejects a directory |

Two suspected defects were **refuted** by reading the installed library source rather
than trusting memory: `Subreddit.submit()` does accept `url=` and `selftext=` together
(the exclusivity check at asyncpraw's `subreddit.py:244` covers only
`gallery/image/poll/url/video`), and the video-thumbnail fallback logo *does* ship
inside the installed package.

### ❌ Confirmed defects

**1. [CRITICAL] A dry-run post permanently suppresses the real one.**
`DRY_RUN=true` is the documented default. The dry-run adapters return genuine
`PostStatus.OK` / `DEGRADED` (`reddit.py:61-68`, `discord.py:81-86`), and
`publish.py:99-101` writes those into `post_results` unconditionally. Nothing in the
schema distinguishes a simulated post from a real one, so `succeeded_platforms`
(`db.py:246`) reports them as already-posted and `build_publishers(skip=already)`
drops the platform. Reproduced end to end: after one dry-run publish,
`succeeded_platforms` returns `{'reddit'}`. Flipping `DRY_RUN=false` — the exact
transition the README tells you to make — means those items never post.
*Fix:* record the dry-run flag on the row and exclude simulated results from the
guard, or skip `record_result` entirely while in dry run.

**2. [HIGH] A mistyped bot token produces a raw 30-line traceback.**
`__main__.py:115` handles a *missing* token cleanly, but `asyncio.run(run(settings))`
at line 133 has no guard, so `telegram.error.InvalidToken` escapes. On Windows this
surfaces as a Python stack trace in the Docker log. *Fix:* catch `InvalidToken` and
print the same style of message used for the missing-token case.

**3. [MEDIUM] A missing `service-account.json` becomes a directory.**
`docker-compose.yml` bind-mounts `./service-account.json`; when the host path does not
exist Docker creates a **directory** there (reproduced). The app still degrades
gracefully, but the directory then blocks saving the real key file, and
`start.bat`'s `if not exist` warning stops firing on later runs.

**4. [MEDIUM] A profile with no platforms enabled marks items FAILED.**
`publish.py:128` guards on `if expected and expected <= succeeded`, so an empty
`expected` falls through to `set_status(FAILED)`. The README states such a profile
"queues, shows approval cards and archives".

**5. [LOW] `data/` was not gitignored.** Fixed in this branch — the Docker path stores
the queue database and downloaded brand media there.

---

## Part 2 — automated audit findings (UNVERIFIED)

Five parallel agents audited the subsystems; four reported before the run was paused.
The adversarial verification pass did **not** complete, so everything below is a
*claim*, not a confirmed defect — with the exception of finding #1, which is
reproduced and promoted to Part 1 above.

Treat these as leads to check, not a to-do list. Three of my own hypotheses this
session were wrong on inspection, and unverified audit output has a similar hit rate.

Coverage: 4 of 5 audit areas reported (33 findings). The deploy/docs area did not finish.

| Severity | Count |
| --- | --- |
| critical | 3 |
| high | 11 |
| medium | 13 |
| low | 6 |

---

## 1. [CRITICAL] Dry-run successes are written to post_results as real successes, so flipping DRY_RUN=false permanently suppresses the real post

**Location:** `app/db.py:246`  
**Category:** data-loss

**Claim:** post_results is the double-post guard, but nothing in the schema (db.py:47-55) or in `record_result` (db.py:221-235) distinguishes a simulated post from a real one. The dry-run adapters return genuine success statuses: reddit.py:63-68 returns `PostStatus.DEGRADED if detail else PostStatus.OK` with url `https://reddit.com/r/{sub}/comments/dry-run/`, and discord.py:81-86 returns the same with `https://discord.com/channels/dry-run/message`. publish.py:99-101 writes those straight into post_results, and `succeeded_platforms` (db.py:246-253, `status IN ('ok','degraded')`) then reports them as "Platforms this item already posted to. Never posted to twice — not on Retry, not after a restart." publish.py:77+81 feeds that set to `build_publishers(skip=already)`, which drops the platform entirely (publish.py:32 `and "reddit" not in skip`, publish.py:39 `and "discord" not in skip`). The README (line 235) tells the user to run in DRY_RUN first and then go live, which is exactly the transition that triggers this.

**Failure scenario:** First run, DRY_RUN=true (the .env.example default), profile has reddit+discord enabled but Reddit credentials not yet obtained. build_publishers logs the warning at publish.py:35 and skips reddit; discord dry-runs OK, so post_results gets ('discord','ok','https://discord.com/channels/dry-run/message'). settle (publish.py:128) sees expected={reddit,discord} not a subset of {discord}, marks the item FAILED and shows the Retry button. The user then obtains Reddit credentials, sets DRY_RUN=false, restarts, and taps 🔁 Retry. `already` = {'discord'} from the dry run, so DiscordPublisher is never constructed. Reddit posts for real; publish.py:104-113 synthesises a fake discord OK result; settle now sees expected ⊆ succeeded, calls archive(POSTED), moves the Drive file to posted/ and deletes the local media. The operator sees "🚀 Posted / ✅ discord — https://discord.com/channels/dry-run/message / already posted — not sent again". Discord never received the post and never can, and the source file has been archived out of the inbox.

**Proposed fix:** Do not record dry-run outcomes as durable successes. Simplest: in publish.py, skip `db.record_result` entirely when `rt.settings.dry_run`. Better: add `dry_run INTEGER NOT NULL DEFAULT 0` to the post_results schema (db.py:47-55), pass the flag through `record_result` (db.py:221), and add `AND dry_run = 0` to the `succeeded_platforms` query (db.py:250) so the guard only ever suppresses real posts.

---

## 2. [CRITICAL] Reddit link and text posts crash on submission.permalink — the post goes live but is recorded as FAILED, and Retry double-posts it

**Location:** `app/platforms/reddit.py:88`  
**Category:** wrong-library-usage / double-post

**Claim:** VERIFIED against asyncpraw 8.0.3 in the venv. For `image=`/`video=` submissions, `Subreddit.submit()` returns a *fetched* Submission (subreddit/subreddit.py:674 -> `await self._reddit.submission(url=url)`, and `Reddit.submission` defaults to `fetch=True`, reddit.py:1007). But for link and self posts, submit ends at subreddit/subreddit.py:1270 `return await self._reddit.post(API_PATH["submit"], data=data)`, and the objector builds an UNFETCHED Submission from `{"id", "name", "drafts_count"}` (objector.py:260-270; note it explicitly `del`s the only URL in the payload). asyncpraw objects do not lazy-load: `RedditBase.__getattr__` (models/reddit/base.py:32-42) raises AttributeError for any attribute not already in `__dict__` when `_fetched` is False. asyncpraw's own docstring says so at subreddit/subreddit.py:1125-1133 ("you must re-fetch the submission"). I reproduced it with the installed library: parsing the exact submit response through `reddit._objector.parsers['t3']` and touching `.permalink` gives `AttributeError: 'Submission' object has no attribute 'permalink'. 'Submission' object has not been fetched, did you forget to execute '.load()'?`. That AttributeError is swallowed by the catch-all at reddit.py:101-103, so the adapter returns `PostResult.failed("reddit", "AttributeError: ...")` for a submission that Reddit already accepted. This is the code path the README advertises as the whole RedGifs workflow (README:397-401, 'A URL in the caption becomes a link post'), and `_choose` (reddit.py:113) routes *any* caption containing a URL down it.

**Failure scenario:** Operator approves an item and writes a caption whose body contains any link (the documented RedGifs flow, or just 'source: https://...'). `_choose` returns "link"; `subreddit.submit(title, url=...)` creates the post on Reddit; line 88 then raises AttributeError; publish records reddit as FAILED (db.record_result with status 'failed'); the Telegram card shows a raw "AttributeError: 'Submission' object has no attribute 'permalink'" and offers 🔁 Retry. Retry re-runs RedditPublisher (succeeded_platforms() excludes 'failed' rows) and `resubmit` defaults to True (subreddit/subreddit.py:1202), so a SECOND identical post is created on the subreddit — and it fails again the same way, forever. The item never archives, so the Drive file is stranded in inbox/.

**Proposed fix:** Fetch before reading attributes, e.g. `submission = await self._submit(...)` then `if not submission._fetched: await submission.load()` — `Submission._fetch_info` uses `self.id`, which the submit response does provide (models/reddit/submission.py:380-385). Safer still, avoid the attribute entirely for link/self posts and build the URL from the id: `f"https://www.reddit.com/r/{config.subreddit}/comments/{submission.id}/"`. Either way, wrap the URL construction so that a post that succeeded is never reported as FAILED — losing the permalink must not turn a live post into a retryable failure.

---

## 3. [CRITICAL] Any startup failure leaves the process HUNG FOREVER (unclosed aiosqlite non-daemon thread), so Docker never restarts it

**Location:** `app/service.py:53`  
**Category:** crash-hang

**Claim:** `conn = await db.connect(...)` (service.py:53) starts aiosqlite's worker thread. In the installed aiosqlite 0.22.1 that thread is NOT a daemon — `.venv/lib/python3.12/site-packages/aiosqlite/core.py:87` is `self._thread = Thread(target=_connection_worker_thread, args=(self._tx,))` with no `daemon=True`, started at core.py:177. It only exits when it receives `_STOP_RUNNING_SENTINEL` from `Connection.stop()`/`close()`.

The only `await conn.close()` is at service.py:86, inside a `finally:` whose `try:` does not begin until service.py:77. Everything between line 53 and line 76 is unguarded: `_build_drive()` (58), `application.initialize()` (62), `application.start()` (63), `updater.start_polling()` (67), `bot.get_me()` (71), `reconcile()` (74). Any exception there propagates out of `run()`, out of `asyncio.run(run(settings))` at `app/__main__.py:133`, the traceback prints — and then the interpreter blocks in `threading._shutdown()` joining the aiosqlite thread, forever.

I reproduced it twice. With a bad token: process printed the traceback and was still alive when `timeout 40` killed it (EXIT=124). `faulthandler` on the minimal repro shows the main thread parked in `threading.py:1622 _shutdown` and the worker in `aiosqlite/core.py:59 _connection_worker_thread`.

Docker consequence: `docker-compose.yml` `restart: unless-stopped` never fires because the process never exits. Docker Desktop shows the container as "Up" while the bot is dead. `windows\stop.bat` (`docker compose down`) waits the full 10s grace period then SIGKILLs.

**Failure scenario:** On the fresh Windows PC, the user pastes a token with a trailing space, or BotFather's token is revoked, or `service-account.json` is malformed. Container starts, prints a 40-line traceback, and then sits in Docker Desktop showing "Up" forever, doing nothing. `logs.bat` shows the traceback but the user assumes it recovered because the container is green. Verified: `TELEGRAM_BOT_TOKEN=1234567890:AAHfake... python -m app` → traceback, then EXIT=124 from `timeout 40`.

**Proposed fix:** Move the connection under a guard: `conn = await db.connect(...)` then `try: ... finally: await conn.close()` covering everything from line 55 to the end of `run()`. Simplest correct shape is `async with contextlib.AsyncExitStack() as stack:` with `stack.push_async_callback(conn.close)` registered immediately after `db.connect`, and the same for `application.shutdown()` registered right after `application.initialize()`.

---

## 4. [HIGH] Every credential error surfaces as a raw traceback — no friendly message anywhere, and the exit is not clean

**Location:** `app/__main__.py:133`  
**Category:** first-run-ux

**Claim:** `asyncio.run(run(settings))` at __main__.py:133 has no `try/except`. `app/service.py:51 run()` has no startup error handling either. Every credential failure therefore reaches `sys.excepthook`.

Confirmed by running the real code:
- Bad `TELEGRAM_BOT_TOKEN` → 40-line traceback ending `telegram.error.InvalidToken: The token \`...\` was rejected by the server.` raised from `service.py:62 await application.initialize()`.
- Malformed / non-JSON `service-account.json` → traceback ending `app.drive.DriveError: cannot read /srv/service-account.json: Expecting value: line 1 column 1 (char 0)`, raised from `service.py:47` via `drive.py:59`.

The other credential errors do not crash but are equally unhelpful on first run:
- Drive folder not shared with the service account → no message at all (see the `check_access` finding).
- Service-account file present but not a Drive key, or Drive API not enabled → `service_account.Credentials.from_service_account_file` / the API call raises inside `asyncio.to_thread`, caught by `app/poller.py:64-65` and re-logged as a full traceback every `poll_interval_seconds` (120s) forever.
- Bad Discord webhook (deleted/rotated) → `app/platforms/discord.py:105` returns `HTTP 401: {...}` as a result card; acceptable.
- Bad Reddit creds → `app/platforms/reddit.py:101-103` returns `OAuthException: ...` as a result card; acceptable.

`__main__.py:115-121` shows the intended pattern (friendly `✗ TELEGRAM_BOT_TOKEN is not set`), but it only covers the *unset* case, never the *wrong* case — which is the one the user will actually hit, since `--check-config` already catches "unset".

**Failure scenario:** User finishes the README credential hunt, double-clicks `start.bat`, then `logs.bat`, and sees a 40-line Python traceback whose only actionable words are buried on the last line. Nothing tells them to re-copy the token from BotFather. Reproduced verbatim above.

**Proposed fix:** Wrap the startup in `app/service.py` (or around line 133 of `__main__.py`) with handlers that map each exception to one actionable line and a non-zero return code, e.g. `except telegram.error.InvalidToken: print('✗ TELEGRAM_BOT_TOKEN was rejected by Telegram. Re-copy it from BotFather (/mybots → API Token) into .env.')`; `except telegram.error.NetworkError` → 'cannot reach api.telegram.org'; `except app.drive.DriveError as e` → 'service-account.json is not readable as JSON — re-download the key from Google Cloud → Service Accounts → Keys'. Return `EXIT_INCOMPLETE` rather than letting the traceback out.

---

## 5. [HIGH] DriveClient.check_access() is never called — an unshared Drive folder produces no error at all, contradicting the README

**Location:** `app/drive.py:148`  
**Category:** silent-misbehaviour

**Claim:** `check_access` (drive.py:148-157) exists, and its docstring says folders are resolved 'at startup rather than on the first poll'. `grep -rn check_access` over the whole repo returns exactly one hit — the definition. It has no callers.

README:314-316 makes this a promise to the operator: '`python -m app` logs the service-account address on startup, and any folder it cannot reach is reported rather than silently skipped. If files never appear, the usual cause is step 10 — the folder was not shared'.

What actually happens: `app/poller.py:24 → drive.py:137 _list_media` issues `files.list(q="'<id>' in parents and trashed = false and (mimeType contains 'image/' ...)")`. A `q` filter can only match files the caller can already see, so an unshared (or wrong-id, or Viewer-shared) folder returns HTTP 200 with `{"files": []}` — no exception, nothing logged. `discover()` returns 0, `poll_once` logs nothing, and the service looks perfectly healthy forever.

The related failure — folder shared as *Viewer* instead of *Editor* — is likewise not detected until `intake.py:171 rt.drive.move()` fails, i.e. only after the item has already been published to Reddit and Discord.

**Failure scenario:** User pastes a folder id from the wrong browser tab, or shares only the three subfolders' parent with the wrong email, or forgets step 10 entirely. They drop 5 files into the Drive inbox and wait. Logs show '@brandbot is online' and 'drive poller started (every 120s)' and then absolute silence. No card ever arrives, no error is ever printed, and there is nothing to search for.

**Proposed fix:** Call it. In `app/service.py`, after `_build_drive`, await `drive.check_access({f'{p.name} inbox': p.drive.inbox_folder_id, f'{p.name} posted': ..., f'{p.name} rejected': ...})` for every profile and log each result at INFO, with a WARNING (or a Telegram message) naming the service-account email for any entry that comes back `UNREACHABLE`. `_folder_name` already raises `DriveError` when the id points at a non-folder, so wrong-id mistakes get caught too.

---

## 6. [HIGH] One cached googleapiclient Resource (one httplib2 connection) is shared across asyncio.to_thread worker threads

**Location:** `app/drive.py:61`  
**Category:** thread-safety

**Claim:** `_client()` (drive.py:61-67) caches a single `Resource` in `self._service` and every thread body reuses it. Those bodies run in *different* threads of the default executor via `asyncio.to_thread` (drive.py:137, 140, 143, 146).

google-api-python-client is not safe for that. Verified in the installed source: `googleapiclient/discovery.py:648` builds one `http` (`_auth.authorized_http(credentials)` wrapping one `build_http()` → one `httplib2.Http`, http.py:1948), stores it at `discovery.py:1438 self._http = http`, and every `HttpRequest.execute()` falls back to it (`googleapiclient/http.py:896-897 if http is None: http = self.http`). `httplib2` caches one connection object per host (`httplib2/__init__.py:1599, 1612, 1622 — conn = self.connections[conn_key] = connection_type(...)`) and its own docstring at line 1342 says 'Not thread-safe, requires external synchronization against concurrent requests.' The underlying `http.client.HTTPConnection` raises `CannotSendRequest` when a second request is started before the first response is read (`/usr/lib/python3.12/http/client.py:1193`).

Concurrency is genuinely reachable here: the poller task can be inside a multi-minute `download()` (drive.py:140) for a large video while the operator taps Approve/Reject, and the PTB handler task runs `intake.py:171 rt.drive.move()` → drive.py:143 on a second worker thread against the same Resource.

Separately, `_client()`'s lazy init at drive.py:62-66 is itself unsynchronised: two threads can both see `self._service is None` and both run `build()`, doing two discovery fetches and leaving one orphaned.

**Failure scenario:** A 300 MB video is downloading from the inbox (thread A, several minutes). The operator taps ✅ Post on an earlier item; `archive()` calls `files().update()` on thread B. Both hit the same pooled HTTPS connection. Result is an intermittent `http.client.CannotSendRequest` / `ResponseNotReady` / `BadStatusLine` — surfaced to the operator as `Drive move failed: ...` (intake.py:178) on an item that actually published fine, or as a spurious `poll failed for profile brand_a` traceback. Intermittent and unreproducible on demand, which is the worst kind to debug on a first setup.

**Proposed fix:** Give each worker thread its own service. Replace the `self._service` attribute with a `threading.local()` holder, or (cheaper) build the credentials once and pass a fresh transport per call: `request.execute(http=google_auth_httplib2.AuthorizedHttp(self._credentials, http=build_http()))`. If neither is palatable, serialise all Drive calls behind a single `threading.Lock` held inside the `_list_media`/`_download`/`_move`/`_folder_name` bodies — but that blocks the poller's downloads behind archive moves.

---

## 7. [HIGH] archive() commits the terminal status and deletes the local media even when the Drive move failed, and the POSTED path discards the error and falsely reports success

**Location:** `app/intake.py:181`  
**Category:** data-integrity

**Claim:** In `archive`, the `except` at intake.py:177-179 records the failure into `problem` but execution falls through unconditionally to `db.set_status(rt.conn, item.id, status)` (intake.py:181) and `cleanup_media(item)` (intake.py:183). The returned `problem` is the only signal that the archive did not happen, and on the POSTED path it is thrown away: publish.py:129 returns it from `settle`, but publish.py:116 calls `await settle(rt, profile, item, results)` and discards the value. handlers.py:309-313 then sends "📁 Drive file moved to posted/." based solely on `settled.status is ItemStatus.POSTED`, with no knowledge of the failure. `_decline` (handlers.py:264-266) does surface `problem`, so the POSTED path is the inconsistent one. Because the file's id is already in `items`, `known_drive_file_ids` (db.py:191-196) contains it and poller.py:28-29 will never re-enqueue it, so there is no recovery path in the UI. A first-run 403 here is very likely: the README instructs the operator to create and share three folders with the service account, and forgetting to share posted/ produces exactly this.

**Failure scenario:** Operator shared inbox/ with the service account but forgot posted/. They approve and post an item. drive.py:114-121 raises HttpError 403 inside intake.py:177; `problem` is set but the item is still marked POSTED (intake.py:181), the local media is deleted (intake.py:183), publish.py:116 drops the error, and handlers.py:312 tells the operator "📁 Drive file moved to posted/." The file is actually still sitting in inbox/, will be listed on every subsequent poll, will be skipped silently by poller.py:29 forever, and the operator has been told the opposite. Recovering requires moving the file by hand in Drive and deleting the row from crosspost.db.

**Proposed fix:** In `archive`, skip `cleanup_media` and do not mark the item terminal when `problem is not None` (or introduce an explicit 'posted, archive pending' state so it can be retried). Change publish.py:116 to `problem = await settle(...)`, return it from `publish_item`, and have handlers.py:309-313 report the real outcome — matching what `_decline` already does at handlers.py:265.

---

## 8. [HIGH] Secret redaction does not cover tracebacks — the bot token is printed in cleartext to docker logs

**Location:** `app/logging_setup.py:22`  
**Category:** secret-leak

**Claim:** `SecretRedactingFilter.filter` (logging_setup.py:22-30) rewrites only `record.msg` and `record.args`. `logging.Formatter.format` appends `self.formatException(record.exc_info)` *after* filters run, and that text is never scrubbed. Nor does the filter apply to `sys.excepthook`, which is not part of logging at all.

This matters because python-telegram-bot 22.8 embeds the raw token in the exception message: `.venv/lib/python3.12/site-packages/telegram/_bot.py:868` → `raise InvalidToken(f"The token \`{self._token}\` was rejected by the server.")`. Exactly the case logging_setup.py's own docstring (lines 1-2) claims to defend against: 'so a credential cannot reach the log even if some library helpfully includes it in an error message'.

Verified two ways:
1. Ran `python -m app` with `TELEGRAM_BOT_TOKEN=1234567890:AAHuniqueSecretTokenABCDEF` and `LOG_LEVEL=DEBUG`. The filter worked for logged records (2 `***REDACTED***` lines, from PTB's `Set Bot API URL` debug), but stderr still contained the raw token exactly once, on the traceback's last line.
2. Direct test of `configure_logging` + `log.exception()` with a token and a Discord webhook URL inside the exception message: both printed in full, unredacted.

The token then lands in `docker compose logs`, in the json-file log on disk (`docker-compose.yml` logging driver), and in anything the user pastes for help — directly contradicting README:131-133 ('It never prints a secret value') and the `scripts/scan-secrets.sh` hygiene story.

**Failure scenario:** User types the token with a typo, runs `logs.bat`, copies the traceback into a GitHub issue or a chat to ask for help. The full bot token goes with it. Anyone holding it controls the bot. Reproduced: `grep -c 'AAHuniqueSecretTokenABCDEF' stderr` → 1.

**Proposed fix:** Do the scrubbing in the Formatter, not the Filter, so it covers exceptions: subclass `logging.Formatter` and scrub the output of `format()` (or override `formatException`) and install it on every handler in `configure_logging`. Additionally install `sys.excepthook = lambda t, v, tb: sys.stderr.write(redactor.scrub(''.join(traceback.format_exception(t, v, tb))))` in `configure_logging`, since fatal errors bypass logging entirely.

---

## 9. [HIGH] FAILED is a dead-end state: its only outgoing transition is Retry, so any deterministic failure traps the item forever

**Location:** `app/models.py:16`  
**Category:** state-machine

**Claim:** Enumerating every write to `status`: QUEUED→PENDING_APPROVAL (intake.py:152 via db.set_offered), PENDING_APPROVAL→{AWAITING_TEXT (handlers.py:253), DECLINED (handlers.py:264), QUEUED (handlers.py:271)}, AWAITING_TEXT→PREVIEWING (db.py:200), PREVIEWING→{POSTING (handlers.py:297), AWAITING_TEXT (handlers.py:279), QUEUED (handlers.py:290)}, POSTING→{POSTED (publish.py:129), FAILED (publish.py:130)}, FAILED→POSTING (handlers.py:222 Action.RETRY). FAILED therefore has exactly one outgoing edge. Action.EDIT and Action.CANCEL both require PREVIEWING (handlers.py:229-230), the result message only ever carries `retry_keyboard` (handlers.py:306), `claim_offerable` only selects `status = QUEUED` (db.py:174) so the poller never touches it, and bot.py:32-33 registers only /start and /test — there is no command to cancel, requeue or edit an item. Deterministic per-item failures are common and are exactly what the first live run produces: a first caption line over 300 chars fails at reddit.py:54-57, a caption over 2000 chars fails at discord.py:51-54, a flair-required subreddit fails at reddit.py:95-100. Each of these fails identically on every Retry. Compounding it, models.py:27-33 deliberately excludes FAILED from `is_terminal`, so `cleanup_media` (intake.py:182-183) is never reached and the local media is retained forever, while the Drive file stays in the inbox but is in `known_drive_file_ids` (db.py:191-196) so poller.py:28-29 will never re-enqueue it.

**Failure scenario:** Operator approves a photo and writes a caption whose first line is a 340-character sentence. Preview shows no warning (intake.py:121-137 checks only Discord's limits). Tap 🚀 Post → reddit.py:55 returns "title is 340 characters, over Reddit's 300" → settle marks the item FAILED → the message offers only 🔁 Retry. Every tap of Retry re-runs the identical request and fails identically. There is no button and no command to edit the text, decline the item, or return it to the queue. The item is stuck permanently, its media is never cleaned up, and its Drive file is stranded in inbox/ and can never be re-queued.

**Proposed fix:** Give FAILED the edges it needs: attach `preview_keyboard(item.id)`-style Edit and Cancel buttons alongside Retry on the result message (handlers.py:306), and widen the `expected` map (handlers.py:224-232) so Action.EDIT accepts FAILED (→ AWAITING_TEXT) and Action.CANCEL/DECLINE accepts FAILED (→ QUEUED or DECLINED). Also add the Reddit 300-char title check to `preview_warnings` (intake.py:121-137) so the common case is caught before Post.

---

## 10. [HIGH] offer_due has no failure backoff, so one un-offerable item permanently consumes a per-cycle slot and re-uploads forever

**Location:** `app/poller.py:50`  
**Category:** correctness

**Claim:** `claim_offerable` returns queued items `ORDER BY id` (db.py:175). poller.py:45 slices `due[: rt.settings.max_offers_per_cycle]` *before* attempting anything, and the handler at poller.py:50-51 logs the exception and leaves the item exactly as it was — still QUEUED with `deferred_until` still NULL, because `db.defer` (db.py:160-164) is the only thing that ever sets that column and it is called only from the Later/Cancel buttons. So a failing item keeps the lowest id, stays at the head of `due` on every poll, and re-consumes one of the (default 5, config.py:22) slots forever. Five such items starve every later item permanently. This is not hypothetical: cards.py:103 only rejects uploads over `FileSizeLimit.FILESIZE_UPLOAD` (50,000,000 — verified in the installed telegram.constants), so a 45 MB video takes the `bot.send_document` path at cards.py:122, and PTB 22.8's HTTPXRequest uses `media_write_timeout=20.0` for media uploads (.venv/lib/python3.12/site-packages/telegram/request/_httpxrequest.py:155). 45 MB in 20 s needs roughly 18 Mbit/s of sustained upstream, which most home connections do not have, so the send raises telegram.error.TimedOut. The operator receives no Telegram message about any of this — the only trace is a traceback in the container log.

**Failure scenario:** User drops a 45 MB clip.mp4 plus ten photos into the Drive inbox. clip.mp4 is item 1. Every 120 s the poller picks items 1-5, spends 20 s re-uploading 45 MB to Telegram, times out, logs a traceback, and offers items 2-5. Once items 2-5 have been approved, item 1 is joined by the next four items — but if two or three more oversized videos are ever added, the head of the queue is entirely poison and no item behind them is ever offered again, while the daemon re-uploads hundreds of MB per hour indefinitely. Nothing appears in Telegram.

**Proposed fix:** On the exception path (poller.py:50-51) call `await db.defer(rt.conn, item.id, backoff_minutes)` with a growing backoff so the item yields its slot, and notify the chat after N consecutive failures. Additionally iterate down `due` until `max_offers_per_cycle` *successful* offers rather than slicing first at poller.py:45, so one bad item cannot block the items behind it within a single cycle.

---

## 11. [HIGH] An enabled platform whose credentials are missing produces no result at all: the item is marked FAILED with no Retry button, and Telegram reports success

**Location:** `app/publish.py:35`  
**Category:** first-run-ux / stuck-state

**Claim:** `build_publishers` skips a configured-but-unset platform with only `log.warning` (publish.py:33-35 for Reddit, 45-50 for Discord). No PostResult is produced, so nothing about Reddit reaches the operator. `settle` (publish.py:125-131) computes `expected = set(profile.enabled_platforms)` which still contains 'reddit', so `expected <= succeeded` is False and the item is set to FAILED without archiving. Back in the handler, `failed = [r for r in results if r.status is PostStatus.FAILED]` (handlers.py:301) is EMPTY — the only result is discord's OK — so `retry_keyboard` is not attached (handlers.py:304) and the message header reads '🚀 Posted' (cards.py:154). This is highly likely on the first real run: the service starts with only TELEGRAM_BOT_TOKEN set (__main__.py:114-121 is the only hard credential gate), and the README hands credentials out 'per phase' with Reddit last.

**Failure scenario:** profiles.yaml has `reddit.enabled: true` and `discord.enabled: true`, but REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD are not yet in .env. Operator taps 🚀 Post. Telegram shows '🚀 Posted' and a green ✅ discord line with a working URL, and no mention of Reddit whatsoever. Meanwhile the item is silently set to FAILED, the Drive file is never moved out of inbox/, no Retry button is offered, and FAILED items are never re-offered (db.claim_offerable only selects QUEUED). The item is unreachable from the UI forever, and the operator believes it published everywhere.

**Proposed fix:** In `build_publishers`, return a `PostResult.failed(name, f"{', '.join(missing)} not set")` (or collect the misconfigurations and have `publish_item` emit them as FAILED results) instead of only logging, so the platform appears in `results`, the operator sees the real reason, and 🔁 Retry is offered once the credentials are added.

---

## 12. [HIGH] A profile with no enabled platforms can never complete: settle always marks it FAILED and never archives, contradicting the documented 'queues and archives only' mode

**Location:** `app/publish.py:128`  
**Category:** state-machine / doc-contradiction

**Claim:** `if expected and expected <= succeeded:` — when `profile.enabled_platforms` is empty the guard short-circuits and control falls to `db.set_status(..., ItemStatus.FAILED)` on line 130, so the archive branch is unreachable. This directly contradicts config.py:310-313 ('A profile with no enabled platforms is legitimate — it queues and archives without publishing, which is exactly the state during buildout'), __main__.py:56 which prints 'none — queues and archives only', and intake.py:135-136 which only warns 'no platforms are enabled, so Post will not publish anywhere'. It is exactly the configuration the README tells a new operator to run during phases 1-3, before any Reddit or Discord credentials exist.

**Failure scenario:** First-run operator follows the phased setup with reddit.enabled=false and discord.enabled=false to test the Telegram/Drive loop. They approve an item and tap 🚀 Post. `publishers` is empty, `results` is empty, `expected` is empty -> the item is set to FAILED instead of POSTED, the Drive file is never moved to posted/, and the local media is never cleaned up. Telegram prints a bare '🚀 Dry run — nothing was published' with no lines and no Retry button (failed list is empty), and since FAILED items are never re-offered by the poller (db.claim_offerable filters on QUEUED) and Retry requires ItemStatus.FAILED via a button that was never rendered, the item is stuck permanently. Every test item accumulates in this state.

**Proposed fix:** Treat an empty `expected` as a completed item: `if expected <= succeeded:` (a subset test against an empty set is already True), so a profile with no platforms archives to posted/ as documented. Keep the FAILED branch for the case where something was expected but did not succeed.

---

## 13. [HIGH] reconcile marks an interrupted post FAILED and tells the operator to tap Retry on a message that was never sent

**Location:** `app/reconcile.py:126`  
**Category:** correctness

**Claim:** `_resume_interrupted_post` sets the item to FAILED (reconcile.py:114) and then instructs "Use 🔁 Retry on the result message once you have checked." (reconcile.py:124), but sends that text with no `reply_markup` (reconcile.py:126). The "result message" it refers to does not exist: the only place `retry_keyboard` is attached is handlers.py:306, which is reached *after* `await publish_item(...)` at handlers.py:300 — and the whole premise of this code path is that the process died inside that call. So no result message with a Retry button was ever delivered. Combined with FAILED having no other inbound path (db.py:174 `claim_offerable` filters `status = QUEUED`; no command re-queues items), the item becomes permanently unreachable from the UI. README lines 108-110 document the intended behaviour ("the message says to check the channel before tapping Retry"), so the code contradicts the docs.

**Failure scenario:** Docker Desktop restarts (or the user closes the laptop) while item 7 is POSTING. On boot, reconcile sends "⚠️ Item 7 (clip.mp4) was mid-post when the service stopped … Use 🔁 Retry on the result message once you have checked." There is no Retry button anywhere in the chat for item 7. The item sits at FAILED forever: the poller skips it (only QUEUED), its media is never cleaned (models.py:33 excludes FAILED from is_terminal), and its Drive file stays in inbox/ but is in known_drive_file_ids so poller.py:29 skips it on every poll. The only recovery is hand-editing crosspost.db.

**Proposed fix:** Pass `reply_markup=retry_keyboard(item.id)` on the send_message at reconcile.py:126 (import it from app.telegram.cards). Also note `_resume` returns at reconcile.py:61-63 before `_ensure_media` runs, so the media is not re-downloaded for a POSTING item — a subsequent Retry would hit publish.py:56-57 with `path=None` and reddit.py:119 would silently downgrade an image post to a text post. Call `_ensure_media` for POSTING items too.

---

## 14. [HIGH] A post text near Telegram's 4096-char limit makes the preview message too long, permanently deadlocking the profile's queue

**Location:** `app/telegram/handlers.py:179`  
**Category:** crash-and-stuck-state

**Claim:** `on_text` writes the caption and flips the item to PREVIEWING (`db.set_caption` -> `status = PREVIEWING`, app/db.py:200) at handlers.py:179, and only *then* sends the preview at handlers.py:182. `send_preview` (app/intake.py:142) sends `preview_text(...)` (app/telegram/cards.py:131-147) as a single `bot.send_message`, and `preview_text` adds ~100 characters of chrome (`"📋 Preview"`, `file:`, `to:`, `title (Reddit):`, `caption:`, the trailing `"Nothing is published until you tap 🚀 Post."`) plus any warning lines on top of the operator's text.

Telegram accepts an incoming text message of up to `MessageLimit.MAX_TEXT_LENGTH` = 4096 (verified: .venv/lib/python3.12/site-packages/telegram/constants.py:2180), so the operator can legitimately send 4096 characters. I ran the real function: `preview_text(filename='a.mp4', title='t', body='b'*4000, platforms=('reddit',), warnings=[])` returns 4106 characters, and with a filename and a Discord warning a 4000-char body produces 4260. PTB does not length-check client-side (`MAX_TEXT_LENGTH` appears only in docstrings in telegram/_bot.py), so Telegram returns 400 and PTB raises `telegram.error.BadRequest: Message is too long`.

That exception is unhandled (no `add_error_handler`, see the separate finding), so the item is left in PREVIEWING with no preview card and no buttons. `db.active_item` (app/db.py:203) counts PREVIEWING as "in flight", so `_approve` (handlers.py:244-251) now refuses *every* other item in that profile with "Finish item N first". Restarting does not help: `reconcile._resume` (app/reconcile.py:75-77) calls `send_preview` again, it raises again, and reconcile swallows it (reconcile.py:56-57). The profile's queue is blocked until someone edits the database by hand.

Note also that README.md:375 claims "Captions over 1024 characters ... the preview sends the media first and the full text as a follow-up message" — no such splitting exists anywhere in the code.

**Failure scenario:** Operator approves an item and sends a 4000-character Reddit self-post body (well within Telegram's 4096 limit). `db.set_caption` moves item 12 to PREVIEWING; `send_preview` builds a 4106-char message and `send_message` raises BadRequest("Message is too long"). The operator sees nothing at all in Telegram. Every subsequent Approve tap on any other item answers "Finish item 12 first — it is still previewing", forever, across restarts.

**Proposed fix:** Bound the preview: in `preview_text` (cards.py:131) clamp the assembled string to `MessageLimit.MAX_TEXT_LENGTH` (truncating the body, not the trailing instruction), or split it into a summary message plus a follow-up body message as the README already promises. Additionally, in `on_text` send the preview *before* committing the status, or wrap handlers.py:179-182 so a send failure rolls the item back to `AWAITING_TEXT` and tells the operator the text was too long.

---

## 15. [MEDIUM] profiles.yaml read only guards FileNotFoundError — a directory (the standard Docker bind-mount trap) crashes with a traceback and returns the wrong exit code

**Location:** `app/config.py:318`  
**Category:** crash

**Claim:** `_parse_profiles_file` does `raw = yaml.safe_load(path.read_text(encoding="utf-8"))` at config.py:318 and catches only `FileNotFoundError` (319) and `yaml.YAMLError` (322). `IsADirectoryError`, `PermissionError` and `UnicodeDecodeError` are all uncaught, so they escape `load_settings` past the `except ConfigError` at __main__.py:106.

This is not hypothetical on the target setup. `docker-compose.yml:12-15` bind-mounts `./profiles.yaml:/srv/profiles.yaml:ro` and `./service-account.json:/srv/service-account.json:ro`. When a bind-mount source does not exist on the host, the Docker daemon creates a **directory** at that path. `windows/check-config.bat` runs `docker compose run --rm --no-deps crosspost ...` with no existence guard at all, and `windows/start.bat` explicitly continues past a missing `service-account.json` ('Drive polling will be off until you add it') straight into `docker compose up -d --build`. README step 4 (check-config) comes before the user has necessarily finished step 3.

I reproduced the code path with a directory at the profiles path: a 12-line traceback ending `IsADirectoryError: [Errno 21] Is a directory: '.../profiles.yaml'`, and **exit code 1** — which README:135 documents as '1 = valid but credentials missing'. So the wrapper scripts and the user are told the config is fine and only credentials are missing, when in fact it is unreadable.

The same trap then makes `service-account.json` a *directory* on the host, so the user cannot later save the downloaded key under that name in Explorer without deleting the folder first — and `service.py:41-44` reports it as the misleading 'no Google service-account file — Drive polling is off'.

**Failure scenario:** User extracts the ZIP to C:\crosspost-engine, double-clicks `windows\check-config.bat` before copying profiles.example.yaml (README step 4 before step 3 is finished). Docker creates C:\crosspost-engine\profiles.yaml as a *folder*, the container prints `IsADirectoryError` and exits 1. Every subsequent attempt to 'copy profiles.example.yaml and rename it to profiles.yaml' in Explorer fails or lands the file inside the folder.

**Proposed fix:** Broaden the handler at config.py:319 to `except OSError as exc: parser.fail(str(path), f'cannot be read: {exc.strerror}'); return {}, []` (OSError covers FileNotFoundError, IsADirectoryError and PermissionError) and add `except UnicodeDecodeError`. Separately, add `if not exist "profiles.yaml"` / `if not exist "service-account.json"` guards to `windows/check-config.bat` the way `start.bat` has them, or create the files with `type nul >` before compose runs so Docker never makes them directories.

---

## 16. [MEDIUM] A Reddit text post with no body raises TypeError from asyncpraw instead of creating a title-only post

**Location:** `app/platforms/reddit.py:147`  
**Category:** wrong-library-usage

**Claim:** `common["selftext"] = request.body or None` (reddit.py:126) collapses an empty body to None, and the self-post branch calls `subreddit.submit(request.title, **common)`. VERIFIED in asyncpraw 8.0.3 (models/reddit/subreddit/subreddit.py:1150-1154): with no gallery/image/poll/url/video, `if kind is None and not (bool(selftext) or selftext == ""): raise TypeError("At least one of 'gallery', 'image', 'poll', 'selftext', 'url', or 'video' must be provided.")`. The library requires the empty string `""` for a title-only submission and rejects None. The one-line-caption case is explicitly supported by the design (base.py:68-75), so this is a supported input hitting an unsupported library call.

**Failure scenario:** An item whose local media is gone (build_request nulls `media.path` when the file is not on disk, publish.py:62 — e.g. after a restart, a manual media/ cleanup, or a failed download) with a one-line caption. `_choose` returns "self"; submit raises TypeError; the catch-all at reddit.py:101 turns it into `PostResult.failed("reddit", "TypeError: At least one of 'gallery', 'image', 'poll', 'selftext', 'url', or 'video' must be provided.")` — a raw library message shown in Telegram that tells the operator nothing about their actual problem.

**Proposed fix:** Pass an empty string rather than None for the text-post branch: build `common` with `"selftext": request.body or ""` for the self case (keep `or None` for media/link posts, where None correctly means 'no body'). Also consider validating this in `preview_warnings` so it is caught before the Post tap.

---

## 17. [MEDIUM] Any URL anywhere in the caption body silently discards the media and turns the post into a link post, reported as a clean success

**Location:** `app/platforms/reddit.py:113`  
**Category:** silent-misbehaviour

**Claim:** `_choose` tests `first_url(request.body)` FIRST, before it even looks at `request.media` (reddit.py:113-120), and returns `("link", None)` — a None detail means `publish` reports PostStatus.OK, not DEGRADED (reddit.py:89-94). `URL_PATTERN = re.compile(r"https?://\S+")` matches a link anywhere in the body, not just a body that *is* a link. The README (397-401) documents this as the manual RedGifs workflow, but the implementation cannot tell 'this URL is where my video lives' from 'this URL is a credit/store link in my caption', and the media that was downloaded, approved and previewed is dropped without a word. Discord, running from the same PostRequest, still attaches the file — so the two platforms silently disagree about what was posted.

**Failure scenario:** Operator uploads a photo and captions it 'New drop\nFull set at https://myshop.example/x'. Reddit gets a link post pointing at the shop with the media never uploaded, while Discord gets the image. The result card shows a plain ✅ reddit with no detail, so nothing signals that the picture was dropped. (And because this is the link path, it also hits the permalink AttributeError above.)

**Proposed fix:** Only treat a caption URL as the post target when there is no local media, or when the URL matches a configured media host — otherwise keep the media post and leave the URL in the body. If the link path is taken while `media.path` is not None, at minimum return a DEGRADED detail such as 'caption contained a link — posted as a link post, the file was not uploaded' so the operator sees it on the result card.

---

## 18. [MEDIUM] A Drive file moved back into the inbox can never be re-queued, and the skip is completely silent

**Location:** `app/poller.py:28`  
**Category:** correctness

**Claim:** Dedupe is keyed on the Drive file id for all time: `drive_file_id TEXT UNIQUE` (db.py:30), `known_drive_file_ids` returns every id ever recorded for the profile regardless of the item's status (db.py:191-196, no status filter), and poller.py:28-29 skips those ids with no log line at all. A Drive re-parent preserves the file id (drive.py:114-121 uses addParents/removeParents, not a copy), so a file the operator declined into rejected/ and then drags back into inbox/ is invisible to the daemon forever. The same holds for the IntegrityError path at db.py:120-121, which returns None with no logging, so poller.py:30 counts nothing and says nothing. Note also that config.py's profile validation (_parse_profiles_file, config.py:337-343) only dedupes `name` and `telegram_chat_id`, not folder ids, so two profiles sharing an inbox folder means the second profile silently never sees any file the first one claimed.

**Failure scenario:** During first-run testing the operator drops test.jpg in the inbox, declines it (it moves to rejected/), fixes their profiles.yaml, then drags test.jpg back into inbox/ to try again. The poller lists it every 120 s and skips it at poller.py:29 with zero output — no log line, no Telegram message. From the operator's point of view Drive polling has silently stopped working, and there is nothing in the logs to explain it.

**Proposed fix:** Log at info level when a listed file is skipped as known (poller.py:28-29) and when db.add_item returns None from the IntegrityError path (db.py:120-121), so the behaviour is at least visible. Then scope the dedupe to what it is actually for: filter `known_drive_file_ids` to non-terminal statuses, or make the uniqueness (profile, drive_file_id) and allow re-enqueue when the previous item for that file is POSTED or DECLINED.

---

## 19. [MEDIUM] settle()'s Drive-move failure is discarded, so the operator is told 'Drive file moved to posted/' when it was not, and the local copy is deleted anyway

**Location:** `app/publish.py:116`  
**Category:** misleading-report / data-handling

**Claim:** `archive()` deliberately returns a human-readable problem string when the Drive re-parent fails (intake.py:155-184: it catches the exception, sets `problem`, then still calls `db.set_status(item, POSTED)` and `cleanup_media(item)` because POSTED is terminal). `settle` faithfully returns that string (publish.py:129), but `publish_item` throws it away at line 116 (`await settle(rt, profile, item, results)`) and returns only `results`. The handler then checks the item's status only, and unconditionally announces the move (handlers.py:307-310: `if settled.status is ItemStatus.POSTED and item.drive_file_id: send '📁 Drive file moved to posted/.'`). The 🗑 Decline path does surface this correctly (handlers.py:264), which shows the publish path is the outlier.

**Failure scenario:** The service account was shared on the inbox folder but not on posted/ (a very common first-run Drive setup mistake, and Drive returns 404/403 for the destination). The post really did publish, so the item goes POSTED, the local media file is unlinked, and Telegram says '📁 Drive file moved to posted/.' — but the file is still sitting in inbox/. Because items.drive_file_id is UNIQUE, the poller never re-queues it (db.add_item returns None), so the file stays in the inbox forever, invisible, while the operator has been told the opposite. Only a WARNING in the container log records the truth.

**Proposed fix:** Have `publish_item` return or propagate the string from `settle` (e.g. `problem = await settle(...)`) and have `_post` append '⚠️ {problem}' instead of the '📁 Drive file moved to posted/.' line when it is non-None, matching what `_decline` already does.

---

## 20. [MEDIUM] DRY_RUN results are written into the dedupe table as real successes and the Drive file is archived for real, so a dry-run item can never be published afterwards

**Location:** `app/publish.py:99`  
**Category:** dry-run-leak / data-loss

**Claim:** In dry run both adapters return OK/DEGRADED with fake URLs (reddit.py:61-68, discord.py:79-86). `publish_item` records those simulated results unconditionally at publish.py:99-101, and `post_results` is exactly what enforces 'never post twice' (db.py:246-253, `succeeded_platforms` matches status IN ('ok','degraded') with no notion of dry run). `settle` then treats them as real successes and calls `archive(..., POSTED)` (publish.py:129), which performs a REAL Drive re-parent — nothing in intake.py or drive.py consults `settings.dry_run` (verified: `dry_run` appears only in config, publish, cards and the two log lines). DRY_RUN=true is the default (config.py:390) and README:235 says it 'simulates the platform calls'.

**Failure scenario:** Operator does the intended DRY_RUN=true rehearsal with three real files in the Drive inbox. All three are moved out of inbox/ into posted/ for real, marked POSTED, their local copies deleted, and post_results now holds 'ok' rows for reddit and discord. They then set DRY_RUN=false to go live: the inbox is empty, so nothing is re-offered, and if they ever reach those items again (Retry, or a re-upload restoring the same file id) publish_item skips both platforms with 'already posted — not sent again'. Nothing was ever published, but the system's state says everything was.

**Proposed fix:** Either skip `db.record_result` / `settle`'s archive when `rt.settings.dry_run` is set, or persist a dry-run marker (e.g. store status 'ok (dry run)' or an extra column) and exclude it from `db.succeeded_platforms`. At minimum, do not perform the real Drive move while in dry run — 'simulates the platform calls' in README:235 should not consume the operator's inbox.

---

## 21. [MEDIUM] reconcile marks Drive-sourced items FAILED and claims they "came from Telegram" whenever the Drive client is unavailable

**Location:** `app/reconcile.py:85`  
**Category:** correctness

**Claim:** `_ensure_media`'s recovery branch at reconcile.py:85 requires `rt.drive` to be truthy. service.py:38-48 sets `rt.drive = None` whenever GOOGLE_SERVICE_ACCOUNT_FILE is unset or `path.is_file()` is false, and explicitly documents that the bot still runs in that state. When that happens, a Drive-sourced item whose local media is gone falls through to the block at reconcile.py:92-99, which is written on the assumption that only Telegram items reach it: it sets the item to FAILED and sends a message asserting the item "came from Telegram, so there is nothing to re-download. Send it again." Both halves are wrong for a Drive item — the bytes are still in Drive and re-downloadable — and the FAILED status is the dead-end described in the other findings. The bind mount at docker-compose.yml (./service-account.json:/srv/service-account.json:ro) makes this easy to hit on a fresh Windows Docker Desktop install: a missing or misnamed host file, or a mount that Docker Desktop has not yet been granted access to, resolves to a non-file and silently disables Drive.

**Failure scenario:** User is mid-flow with three Drive items awaiting approval, then renames service-account.json on the Windows host (or Docker Desktop drops the file-sharing grant) and the container restarts. service.py:41 logs the warning and sets drive=None. reconcile then marks all three Drive items FAILED and sends three messages telling the operator they came from Telegram and to send them again. The items are now permanently stuck in FAILED (nothing re-queues FAILED), and the guidance given is factually wrong.

**Proposed fix:** Branch on `item.source` before deciding the message: if `item.source is ItemSource.DRIVE`, leave the status untouched and return False with a message saying Drive is not configured / the file could not be re-downloaded and that the item stays queued. Reserve the FAILED path and the "send it again" wording for `ItemSource.TELEGRAM`.

---

## 22. [MEDIUM] --check-config reports 'Configuration complete' for a service-account.json that is not a usable key, and a malformed one is fatal at runtime instead of degrading

**Location:** `app/service.py:47`  
**Category:** first-run-ux

**Claim:** Two halves of the same gap.

(a) `Settings.missing_files()` (config.py:146-158) only tests `self.google_service_account_file.is_file()`. I ran `--check-config` with `GOOGLE_SERVICE_ACCOUNT_FILE` pointing at a file containing `{}` and everything else set: it printed 'Configuration complete.' and returned 0. A user who downloaded the wrong file, got a truncated download, or saved the OAuth *client* JSON instead of the *service-account* key gets a green light from the one tool that exists to prevent that.

(b) `_build_drive` (service.py:38-48) is written to degrade gracefully — service.py:41-44 warns and returns `None` when the file is missing. But line 47, `log.info("drive service account: %s", client.account_email)`, calls a property that raises `DriveError` on `OSError`/`JSONDecodeError` (drive.py:57-59). It is not inside any try. So a malformed key file is *fatal* (and, per the first finding, hangs the process), while a missing one is merely a warning — the inverse of what the code intends. Verified: `service-account.json` containing `not json at all` → `app.drive.DriveError: cannot read ...` traceback, then EXIT=124 from `timeout 25`.

Minor related point: `account_email` also does blocking file I/O directly on the event loop (drive.py:57 inside async `run()`); tiny in absolute terms, but it is the one Drive call in the codebase that skips `asyncio.to_thread`, contrary to the module docstring at drive.py:6-8 ('every call is pushed through asyncio.to_thread').

**Failure scenario:** User clicks 'Add key' in Google Cloud, the browser saves a 0-byte or HTML error page as service-account.json, or they grab the OAuth client_secret json by mistake. check-config.bat says 'Configuration complete.' They start the engine; it dies with a DriveError traceback and hangs, with no hint that the JSON is the problem.

**Proposed fix:** In `_build_drive`, wrap lines 45-48 in `try/except DriveError` and fall back to the same warning-and-return-None path used for a missing file, naming the file. In `Settings.missing_files()` (config.py:153), also `json.loads` the file and check it has `type == 'service_account'` and a `client_email`, reporting e.g. 'GOOGLE_SERVICE_ACCOUNT_FILE is not a Drive service-account key (no client_email) — re-download from Cloud Console → Service Accounts → Keys' so `--check-config` returns 1 instead of 0.

---

## 23. [MEDIUM] No error handler is registered, so every handler exception is a log-only traceback and the operator sees nothing

**Location:** `app/telegram/bot.py:38`  
**Category:** first-run-ux

**Claim:** `build_application` (bot.py:28-42) adds handlers but never calls `application.add_error_handler(...)`. Verified in .venv/lib/python3.12/site-packages/telegram/ext/_application.py:1939: with no error handler PTB just logs `"No error handlers are registered, logging exception."` and returns.

On a first run against real credentials this is the difference between a diagnosable failure and a silent one. Every handler in handlers.py performs its database write *before* its last network call, so an exception leaves a half-applied state with no user-visible feedback:
- `_approve` (handlers.py:253-259): status is already AWAITING_TEXT when `close_card` or the ForceReply send fails, so the prompt never appears and the queue is blocked.
- `_post` (handlers.py:297-300): status is already POSTING when `close_card` fails, so `publish_item` never runs and the item is wedged in POSTING until a restart.
- `on_media` (handlers.py:148-160): the row is already inserted as QUEUED when `get_file()`/`download_to_drive` fails (bad network, Telegram 400 "file is too big" when `file_size` was absent), so the operator is told nothing.

The operator is on a fresh Windows PC watching `docker compose logs`; a bare traceback with no Telegram-side message is exactly the failure mode the design elsewhere tries to avoid.

**Failure scenario:** Operator taps ✅ Approve. `close_card`'s `edit_caption` fails (network blip, or the card was deleted -> BadRequest "Message to edit not found"). PTB logs a traceback; Telegram shows nothing. Item 7 is now AWAITING_TEXT with no prompt, and every later Approve answers "Finish item 7 first — it is still awaiting_text".

**Proposed fix:** Register an error handler in `build_application`: log the exception and, when `update.effective_chat` is allow-listed, send a short "⚠️ Something went wrong handling that — check the logs" message so the failure is visible where the operator is looking. Also consider ordering each handler so the database write happens after (or is rolled back on failure of) the Telegram call.

---

## 24. [MEDIUM] `close_card` truncates edited text messages at the 1024-char caption limit, silently chopping the preview and discarding the outcome banner

**Location:** `app/telegram/cards.py:175`  
**Category:** correctness

**Claim:** `truncate_caption` (cards.py:74-76) always uses `MessageLimit.CAPTION_LENGTH` = 1024. `close_card` applies it on both branches, including cards.py:175 where it calls `edit_text` on a plain text message, whose real limit is `MessageLimit.MAX_TEXT_LENGTH` = 4096 (verified in telegram/constants.py:2180/2193).

Because the outcome is *appended* (`updated = f"{original}\n\n{outcome}"`, cards.py:170), truncation removes the outcome first. I ran it: a preview message of 1625 characters (title + a 1500-char body) plus "🚀 Posting…" comes back as exactly 1024 characters ending in `BBBB…`, with `'Posting' in closed == False`. So after tapping 🚀 Post the operator's preview card is cut in half and carries no indication that anything happened — the buttons vanish and that is the only feedback. The same applies to `_cancel`, `_edit`, and the "⚠️ Ignored — item is X, not Y" path at handlers.py:236.

The same wrong constant is used at cards.py:113, where a `bot.send_message` body is truncated to 1024 instead of 4096.

**Failure scenario:** Operator writes a 1400-character caption. Preview renders fine (1625 chars). Operator taps 🚀 Post: the preview card is rewritten to 1024 characters, the body is cut mid-word, and "🚀 Posting…" is nowhere to be seen. The operator cannot tell whether the tap registered.

**Proposed fix:** Give `truncate_caption` a `limit` parameter (or add a `truncate_text` using `MessageLimit.MAX_TEXT_LENGTH`) and use the text limit on the `edit_text`/`send_message` paths (cards.py:113, cards.py:175). Better still, truncate `original` and append `outcome` afterwards so the outcome can never be the part that is dropped.

---

## 25. [MEDIUM] `_post` announces "Drive file moved to posted/" even when the Drive move actually failed

**Location:** `app/telegram/handlers.py:310`  
**Category:** data-integrity-misreport

**Claim:** `archive` (app/intake.py:164-184) catches any Drive failure into a `problem` string but still runs `await db.set_status(rt.conn, item.id, status)` at intake.py:181, so the item becomes POSTED whether or not the re-parent succeeded. `settle` returns that problem string (app/publish.py:129) but `publish_item` discards the return value at publish.py:116. `_post` then checks only `settled.status is ItemStatus.POSTED and item.drive_file_id` (handlers.py:310) and unconditionally sends "📁 Drive file moved to posted/."

So a permissions error, a wrong `posted_folder_id`, or an expired service-account token — all extremely likely on a first real run — produce a message asserting the file was archived when it is still sitting in `inbox/`. It will never be re-queued either, because `db.known_drive_file_ids` (app/db.py:191) already contains its id, so it silently accumulates in the inbox. `_decline` does surface the problem string (handlers.py:265); the Post path is the one that drops it.

**Failure scenario:** Service account has viewer-only access to the `posted/` folder. Operator taps 🚀 Post; Reddit and Discord both succeed; `rt.drive.move` raises 403; `archive` logs it and returns "Drive move failed: ..." but still sets POSTED. `publish_item` throws the string away. The operator is told "📁 Drive file moved to posted/." The file remains in `inbox/` and is never offered again.

**Proposed fix:** Have `publish_item` return (or `settle` propagate) the archive problem, and in `_post` report it: `if settled.status is POSTED and item.drive_file_id: text = '📁 Drive file moved to posted/.' if problem is None else f'⚠️ {problem}'`. Mirror the `_decline` pattern at handlers.py:264-266.

---

## 26. [MEDIUM] Decline tells the operator "Moved to rejected/" even when there is no Drive file or Drive is not configured

**Location:** `app/telegram/handlers.py:265`  
**Category:** first-run-ux

**Claim:** `_decline` builds its message as `"❌ Declined\nMoved to rejected/." if problem is None else ...` (handlers.py:265). `archive` (app/intake.py:164) only attempts a move when `item.drive_file_id and rt.drive and profile` — otherwise it falls straight through to `db.set_status` at intake.py:181 and returns `None`. So `problem is None` is also the value returned when nothing was moved at all.

Two cases hit this on the very first run, which is precisely the phase the README says the bot is usable in ("Without a service-account file the bot still runs ... media sent directly to it still queues", README.md:57-59):
1. Any item created by `on_media` or `/test` (`ItemSource.TELEGRAM`, `drive_file_id = None`) — the README itself says these are "never archive-moved" (README.md:71-72).
2. Any item at all while `GOOGLE_SERVICE_ACCOUNT_FILE` is unset, so `rt.drive is None` (app/service.py:38-48).

The operator is told a Drive archive happened that did not, which is actively misleading while they are trying to confirm Drive is wired up correctly.

**Failure scenario:** Fresh install, no Google credentials yet. Operator sends a photo to the bot and taps ❌ Decline. The card is edited to "❌ Declined / Moved to rejected/." The operator goes looking in Drive for a `rejected/` folder that was never touched — and reasonably concludes Drive integration is working when it is not even configured.

**Proposed fix:** Branch on whether a move was actually attempted, e.g. `moved = bool(item.drive_file_id and rt.drive)`, and say "Moved to rejected/." only when `moved and problem is None`; otherwise "❌ Declined" (plus "local copy removed") for Telegram-sourced items.

---

## 27. [MEDIUM] `on_text` ignores `reply_to_message`, so a reply to a stale ForceReply prompt is applied to a different item

**Location:** `app/telegram/handlers.py:170`  
**Category:** wrong-item-attribution

**Claim:** `_approve` (handlers.py:255-259) and `_edit` (handlers.py:281-285) both send their prompt with `ForceReply(...)`, which makes the Telegram client open a reply composer targeting that specific message. But `on_text` never looks at `update.effective_message.reply_to_message`; it resolves the target purely by `db.active_item(rt.conn, profile.name)` (handlers.py:170) — "whichever item happens to be AWAITING_TEXT right now".

So the UI invites the operator to reply to a particular prompt while the code ignores which prompt was replied to. Old prompts stay in the chat history forever (nothing removes their ForceReply affordance), and Telegram happily lets you reply to any message in the scrollback. The item's own card message id *is* stored (`items.telegram_message_id`, set in `db.set_offered`, app/db.py:149) but the prompt message id is not stored at all, so there is nothing to cross-check against.

The consequence is publishing the wrong text: `on_text` writes it straight to the current active item (handlers.py:179) and previews it, and the preview looks entirely correct, so the mistake is not obvious before tapping 🚀 Post.

**Failure scenario:** Operator approves item 4, sends text, sees the preview, and taps ✖️ Cancel (item 4 -> QUEUED, handlers.py:290). The poller offers item 5; the operator approves it and a new prompt appears. They then scroll up, tap Reply on item 4's old prompt, and type item 4's caption. `active_item` returns item 5, so item 5 gets item 4's title and body, and the preview shows item 5's filename with item 4's text — which is easy to miss and publishes the wrong caption to Reddit and Discord.

**Proposed fix:** Record the prompt's `message_id` when `_approve`/`_edit` send it, and in `on_text` reject (with a short explanation) any `message.reply_to_message` whose id is not the current item's prompt. At minimum, when `message.reply_to_message` is present and does not match the active item's prompt, tell the operator which item the text would apply to instead of silently applying it.

---

## 28. [LOW] later_cooldown_minutes: 0 is silently rewritten to 60, and negative values are accepted and re-offer the item every cycle

**Location:** `app/config.py:393`  
**Category:** silent-misbehaviour

**Claim:** `later_cooldown_minutes=later_cooldown or DEFAULT_LATER_COOLDOWN_MINUTES` (config.py:393). An explicit `later_cooldown_minutes: 0` parses fine (`parser.integer` at config.py:375-377 returns 0), then `0 or 60` evaluates to 60. The operator's setting is discarded with no message, and `app/telegram/handlers.py:273` then tells them 'Back in about 60 min.'

Unlike its two neighbours, this value has no range check: config.py:372-373 rejects `poll_interval_seconds < 10` and config.py:381-382 rejects `max_offers_per_cycle < 1`, but nothing validates the cooldown. A negative value passes through untouched, so `db.defer` (db.py:163) writes a `deferred_until` timestamp in the past, `claim_offerable`'s `deferred_until <= now` predicate (db.py:173) is immediately true, and the same card is re-sent every poll cycle.

**Failure scenario:** Operator sets `later_cooldown_minutes: 0` meaning 'put it back and offer it again next cycle'; the bot ignores it and says 60 minutes. Or they set `-1` experimenting, tap ⏭ Later once, and the card is re-posted to their Telegram chat every 120 seconds until they act on it.

**Proposed fix:** Use `later_cooldown if later_cooldown is not None else DEFAULT_LATER_COOLDOWN_MINUTES` at config.py:393 (same for the other two `or` fallbacks at 392/394, which are only safe because of their range checks), and add `if later_cooldown is not None and later_cooldown < 0: parser.fail('config.later_cooldown_minutes', 'cannot be negative')` next to the existing checks.

---

## 29. [LOW] add_item swallows IntegrityError without rolling back, leaving an uncommitted transaction on the shared connection

**Location:** `app/db.py:120`  
**Category:** correctness

**Claim:** Python's sqlite3 issues an implicit BEGIN before a DML statement. When the INSERT at db.py:111-119 raises IntegrityError, db.py:120-121 returns None without a rollback or commit, so the transaction stays open. Verified empirically against the installed aiosqlite 0.22.1 / Python 3.12.3: after a duplicate `add_item`, `conn._conn.in_transaction` is True. This is the one code path that breaks the invariant the module docstring relies on (db.py:7-8: "Every call here is a single statement plus commit … so short atomic operations stay safe when several tasks share a connection"). The single connection is shared between the poller task and the PTB handler tasks (service.py:57-58, 76), and every read issued between the swallowed IntegrityError and the next write runs inside that stale snapshot. In WAL mode (db.py:68) a held read snapshot also blocks checkpointing, so crosspost.db-wal grows until the next write commits.

**Failure scenario:** Two profiles are pointed at the same Drive inbox folder (nothing in config.py rejects this). Profile A's discover() enqueues file F; profile B's discover() hits the UNIQUE constraint, IntegrityError is swallowed, and the transaction is left open. Every SELECT after that — claim_offerable, known_drive_file_ids, get_item — reads from that snapshot until an unrelated Telegram action performs a write and implicitly commits it, and the WAL file grows in the meantime.

**Proposed fix:** Add `await conn.rollback()` before `return None` in the except block at db.py:120-121, or replace the try/except with `INSERT ... ON CONFLICT (drive_file_id) DO NOTHING` and treat `cursor.rowcount == 0` as the already-known case.

---

## 30. [LOW] Failed downloads leave orphaned .part files in the media volume forever

**Location:** `app/drive.py:106`  
**Category:** resource-leak

**Claim:** `_download` writes to `destination.with_suffix(destination.suffix + '.part')` (drive.py:106) and only renames it into place at drive.py:112 after `next_chunk()` finishes. If the chunk loop raises — network drop, token expiry, `docker stop` mid-download, disk full — the partial file stays.

Nothing ever removes it. `intake.cleanup_media` (intake.py:187-196) unlinks only `item.local_path`, and `db.set_local_path` is called at intake.py:99 *after* `download()` returns, so a failed download never records a path at all. The retry path (`fetch_media`, intake.py:91) checks `Path(item.local_path).is_file()` for the final name, so it correctly re-downloads — writing a fresh `.part` over the old one for that item, but leaving behind any `.part` from an item that was later rejected or abandoned.

**Failure scenario:** A 400 MB video download is interrupted by `stop.bat` three times; the operator then rejects the item. `data/media/brand_a/17-clip.mp4.part` stays on disk indefinitely. Over months of flaky downloads the Docker volume fills with partials the user has no reason to look for.

**Proposed fix:** Wrap the download body in `try/except BaseException: partial.unlink(missing_ok=True); raise`, and/or sweep `*.part` older than a day from `settings.media_dir` at startup.

---

## 31. [LOW] Discord text-only posts are sent as application/x-www-form-urlencoded, not JSON or multipart

**Location:** `app/platforms/discord.py:95`  
**Category:** wire-format-risk

**Claim:** VERIFIED with the installed httpx 0.28.1: `client.post(url, data={"payload_json": ...}, files=None)` encodes the body as `application/x-www-form-urlencoded` (`payload_json=%7B%22content%22...`), while the same call with `files={...}` correctly produces `multipart/form-data`. So the file path is fine and the fileless path leaves the two body formats Discord documents for Execute Webhook (application/json and multipart/form-data); `payload_json` is documented as a form-data field. I could not test this against the live API, so treat it as a risk rather than a confirmed break — but the fix is two lines and removes the uncertainty before the operator burns a setup session on it.

**Failure scenario:** Any post with no attachable file — media missing, or media over the 10 MiB ceiling (discord.py:60-70, the degraded video path the README advertises at 403-404) — goes out urlencoded. If Discord does not honour `payload_json` outside multipart it responds 400 ('Cannot send an empty message'), `raise_for_status` fires, and the operator sees 'HTTP 400: ...' for every text-only post while image posts under 10 MiB succeed — a confusing, intermittent-looking failure.

**Proposed fix:** Branch on whether there is a file: with a file keep `data={"payload_json": ...}` + `files=...`; without one send `json={"content": content}` so the request goes out as application/json, which Discord documents unambiguously.

---

## 32. [LOW] Size-limit messages print 19.1 MB / 47.7 MB, contradicting the 20 MB / 50 MB figures in the README and docstrings

**Location:** `app/telegram/handlers.py:143`  
**Category:** misleading-message

**Claim:** `FileSizeLimit.FILESIZE_DOWNLOAD` is `int(20e6)` = 20,000,000 and `FILESIZE_UPLOAD` is `int(50e6)` = 50,000,000 — decimal MB (verified in .venv/lib/python3.12/site-packages/telegram/constants.py, class FileSizeLimit). `format_size` (cards.py:65-71) divides by 1024, so I get: `format_size(FILESIZE_DOWNLOAD)` -> "19.1 MB", `format_size(FILESIZE_UPLOAD)` -> "47.7 MB", `format_size(PHOTOSIZE_UPLOAD)` -> "9.5 MB".

Those strings are rendered directly to the operator at handlers.py:143 ("Telegram only lets a bot download up to 19.1 MB") and cards.py:108 ("exceeds Telegram's 47.7 MB upload limit"). Both contradict the same file's own docstrings (handlers.py:115-117 "caps what a bot may download at 20 MB"; cards.py:96-99 "photos cap at 10 MB, any other upload at 50 MB") and README.md:363-366 ("Telegram bot upload | 10 MB photos, 50 MB other files", "download (getFile) | 20 MB").

During setup, a user who sees "19.1 MB" after reading "20 MB" reasonably concludes the code is using a different (wrong) limit and starts debugging a non-bug.

**Failure scenario:** Operator DMs the bot a 19.5 MB video to test the size guard. The bot replies "…is 18.6 MB. Telegram only lets a bot download up to 19.1 MB, so I cannot fetch it." — a refusal quoting a limit the operator has never seen documented, for a file the docs imply is under the limit.

**Proposed fix:** Either report these constants with a decimal formatter (divide by 1000 for limits sourced from `FileSizeLimit`), or state them as the documented round figures in the message text and keep `format_size` only for actual file sizes.

---

## 33. [LOW] Telegram-sourced items are inserted as QUEUED before download, so the Drive poller can offer the same item a second time

**Location:** `app/telegram/handlers.py:148`  
**Category:** duplicate-card

**Claim:** `on_media` inserts the row with `enqueue_telegram_media` at handlers.py:148 — `db.add_item` writes `status = ItemStatus.QUEUED` (app/db.py:118) — and only sets it to PENDING_APPROVAL later, via `offer` -> `db.set_offered`, at handlers.py:160. Between those two lines it awaits `media.get_file()` and `download_to_drive` (handlers.py:155-156), which for a file near the 20 MB cap can take tens of seconds.

`db.claim_offerable` (app/db.py:167) selects on `profile = ? AND status = QUEUED` with no source filter, so the concurrently-running poller task (`poll_forever`, app/poller.py:68) can pick the item up in that window. `offer_due` calls `fetch_media`, which returns `None` for `ItemSource.TELEGRAM` (app/intake.py:93), and then `offer` anyway — producing a card that says "⚠️ Media not available locally" and setting PENDING_APPROVAL. When `on_media` finishes it calls `offer` again, producing a second card for the same item.

The same window means that if the download raises (a Telegram 400 when `file_size` was absent, so the `size > FILESIZE_DOWNLOAD` guard at handlers.py:140 was bypassed with `size = 0`, or a transient network error), the row is permanently left QUEUED with no local media and — with no error handler — no notice to the operator. The poller will then offer it as a media-less card that can be approved and published as text only.

**Failure scenario:** Operator DMs an 18 MB video while a poll cycle is due. The poller sees item 9 as QUEUED, sends a card reading "⚠️ Media not available locally", and sets PENDING_APPROVAL. Seconds later `on_media` finishes downloading and sends a second card for item 9. Two live approval cards exist; approving from the first one publishes without the media.

**Proposed fix:** Insert Telegram-sourced items in a state the poller does not claim (e.g. add a `DOWNLOADING` status, or set `deferred_until` far in the future and clear it in `offer`), or scope `claim_offerable` to `source = 'drive'`. Also wrap handlers.py:155-160 so a download failure marks the item FAILED and tells the operator, rather than leaving a media-less row in the queue.
