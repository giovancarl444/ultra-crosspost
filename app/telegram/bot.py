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


def build_application(settings: Settings) -> Application:
    allowed_chat_ids = [profile.telegram_chat_id for profile in settings.profiles]
    allowed = filters.Chat(chat_id=allowed_chat_ids)

    application = (
        ApplicationBuilder().token(settings.telegram_bot_token.resolve()).build()
    )
    application.bot_data[handlers.PROFILES_BY_CHAT] = {
        profile.telegram_chat_id: profile for profile in settings.profiles
    }

    application.add_handler(CommandHandler("start", handlers.start, filters=allowed))
    application.add_handler(CommandHandler("test", handlers.test, filters=allowed))
    application.add_handler(CallbackQueryHandler(handlers.on_button))
    application.add_handler(
        MessageHandler(~allowed, handlers.log_ignored), group=IGNORED_GROUP
    )

    log.info("bot configured for %d allow-listed chat(s)", len(allowed_chat_ids))
    return application
