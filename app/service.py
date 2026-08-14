"""Composition root: start everything under one event loop and stop it cleanly.

The Telegram Application is driven manually rather than through `run_polling()`, which
would take ownership of the loop. That leaves the Drive poller free to run as a sibling
task, and lets both shut down in order on a signal.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram import Update

from app import db
from app.config import Settings
from app.drive import DriveClient
from app.intake import Runtime
from app.poller import poll_forever
from app.reconcile import reconcile
from app.telegram.bot import build_application
from app.telegram.handlers import RUNTIME

log = logging.getLogger(__name__)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows outside a container has no add_signal_handler for these.
            signal.signal(sig, lambda *_: stop.set())


def _build_drive(settings: Settings) -> DriveClient | None:
    path = settings.google_service_account_file
    if path is None or not path.is_file():
        log.warning(
            "no Google service-account file — Drive polling is off. "
            "Media sent directly to the bot still queues."
        )
        return None
    client = DriveClient(path)
    log.info("drive service account: %s", client.account_email)
    return client


async def run(settings: Settings) -> None:
    application = build_application(settings)
    conn = await db.connect(settings.database_path)
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    runtime = Runtime(
        settings=settings, conn=conn, bot=application.bot, drive=_build_drive(settings)
    )
    application.bot_data[RUNTIME] = runtime

    await application.initialize()
    await application.start()
    # Pending updates are kept, not dropped: a tap made while the service was down is a
    # real instruction, and every action already guards against the item's current status,
    # so a replayed tap answers "already handled" instead of acting twice.
    await application.updater.start_polling(
        drop_pending_updates=False, allowed_updates=Update.ALL_TYPES
    )

    me = await application.bot.get_me()
    log.info("@%s is online — %s", me.username, "DRY RUN" if settings.dry_run else "LIVE")

    await reconcile(runtime)

    poller = asyncio.create_task(poll_forever(runtime, stop), name="drive-poller")
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await conn.close()
        log.info("stopped cleanly")
