"""Logging setup with a redaction filter, so a credential cannot reach the log even if
some library helpfully includes it in an error message or a request URL."""

from __future__ import annotations

import logging

REDACTED = "***REDACTED***"
MIN_REDACTABLE_LENGTH = 8
"""Short values are not redacted — scrubbing a 3-character string would mangle log
lines all over the place without protecting anything meaningful."""


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Longest first, so a token that contains another value is masked as a whole.
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= MIN_REDACTABLE_LENGTH}, key=len, reverse=True
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = self._scrub(str(record.msg))
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            scrubbed = tuple(self._scrub(str(a)) if isinstance(a, str) else a for a in args)
            record.args = scrubbed if isinstance(record.args, tuple) else scrubbed[0]
        return True

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = SecretRedactingFilter(secrets or [])
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)

    # These are chatty at INFO and say nothing useful about our own flow.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
