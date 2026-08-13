"""Composition root: start everything under one event loop and stop it cleanly.

The Telegram Application is driven manually rather than through `run_polling()`, which
would take ownership of the loop. From Phase 2 the Drive poller runs as a sibling task
here, and both shut down in order on a signal.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram import Update

from app.config import Settings
from app.telegram.bot import build_application

log = logging.getLogger(__name__)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows outside a container has no add_signal_handler for these.
            signal.signal(sig, lambda *_: stop.set())


async def run(settings: Settings) -> None:
    application = build_application(settings)
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    await application.initialize()
    await application.start()
    # drop_pending_updates so a restart does not replay taps that were already handled.
    await application.updater.start_polling(
        drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
    )

    me = await application.bot.get_me()
    log.info("@%s is online — %s", me.username, "DRY RUN" if settings.dry_run else "LIVE")

    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        log.info("stopped cleanly")
