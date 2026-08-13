"""Builds the Telegram Application and wires the allow-list."""

from __future__ import annotations

import logging

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.telegram import handlers

log = logging.getLogger(__name__)

IGNORED_GROUP = 1
"""Handlers in group 0 serve allow-listed chats. The ignored-chat logger sits in its own
group so it still runs for everything else — PTB dispatches at most one handler per group."""

MEDIA = filters.PHOTO | filters.VIDEO | filters.Document.ALL


def build_application(settings: Settings) -> Application:
    allowed = filters.Chat(chat_id=[p.telegram_chat_id for p in settings.profiles])
    application = ApplicationBuilder().token(settings.telegram_bot_token.resolve()).build()

    application.add_handler(CommandHandler("start", handlers.start, filters=allowed))
    application.add_handler(CommandHandler("test", handlers.test, filters=allowed))
    application.add_handler(MessageHandler(allowed & MEDIA, handlers.on_media))
    application.add_handler(CallbackQueryHandler(handlers.on_button))
    application.add_handler(MessageHandler(~allowed, handlers.log_ignored), group=IGNORED_GROUP)

    log.info("bot configured for %d allow-listed chat(s)", len(settings.profiles))
    return application
