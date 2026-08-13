"""Entrypoint. `python -m app --check-config` validates the setup without any network use."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app import __version__
from app.config import ConfigError, Profile, Settings, load_settings
from app.logging_setup import configure_logging

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_INVALID_CONFIG = 2

OK = "✓"
BAD = "✗"


def _section(enabled: bool, detail: str) -> str:
    return f"{'enabled ' if enabled else 'disabled'}  {detail if enabled else '-'}"


def _describe_profile(profile: Profile) -> list[str]:
    reddit, discord, redgifs = profile.reddit, profile.discord, profile.redgifs

    reddit_detail = (
        f"r/{reddit.subreddit}  nsfw={'yes' if reddit.nsfw else 'no'}  "
        f"flair={reddit.flair_text or reddit.flair_id or 'none'}"
    )
    discord_detail = f"webhook from {discord.webhook.env_var}" if discord.webhook else "?"
    redgifs_detail = (
        f"video host  tags={', '.join(redgifs.default_tags) or 'none'}  "
        f"private={'yes' if redgifs.private else 'no'}"
    )

    rows = [
        ("telegram chat", str(profile.telegram_chat_id)),
        ("drive inbox", profile.drive.inbox_folder_id),
        ("drive posted", profile.drive.posted_folder_id),
        ("drive rejected", profile.drive.rejected_folder_id),
        ("reddit", _section(reddit.enabled, reddit_detail)),
        ("discord", _section(discord.enabled, discord_detail)),
        ("redgifs", _section(redgifs.enabled, redgifs_detail)),
        ("x", _section(profile.x.enabled, "") if profile.x.enabled else "disabled  deferred, see README"),
    ]
    return [f"  {label:<15}{value}" for label, value in rows]


def check_config(settings: Settings) -> int:
    print(f"Crosspost Engine {__version__} — configuration check\n")
    print(f"  profiles file  {settings.profiles_file}")
    print(f"  mode           {'DRY RUN — nothing is published' if settings.dry_run else 'LIVE'}")
    print(f"  poll interval  {settings.poll_interval_seconds}s")
    print(f"  media dir      {settings.media_dir}")
    print(f"  database       {settings.database_path}")

    for profile in settings.profiles:
        enabled = ", ".join(profile.enabled_platforms) or "none"
        print(f"\nprofile: {profile.name}  ({enabled})")
        print("\n".join(_describe_profile(profile)))

    missing_secrets = settings.missing_secrets()
    missing_files = settings.missing_files()

    print("\ncredentials")
    for ref in [
        settings.telegram_bot_token,
        *(r for p in settings.profiles for r in p.secrets()),
    ]:
        mark = OK if ref.is_set else BAD
        print(f"  {mark} {ref.env_var:<34}{ref.purpose}")

    for problem in missing_files:
        print(f"  {BAD} {problem}")

    outstanding = len(missing_secrets) + len(missing_files)
    if outstanding:
        print(
            f"\nConfiguration is valid but incomplete: {outstanding} credential(s) still needed."
            "\nSet them in .env (see .env.example) — values are never printed."
        )
        return EXIT_INCOMPLETE

    print("\nConfiguration complete.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Human-approved multi-platform crossposting.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="parse the config, print the profiles and report missing credentials, then exit",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=None,
        help="path to profiles.yaml (default: $PROFILES_FILE or ./profiles.yaml)",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.profiles)
    except ConfigError as exc:
        print(f"Configuration is invalid ({len(exc.problems)} problem(s)):\n", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {BAD} {problem}", file=sys.stderr)
        return EXIT_INVALID_CONFIG

    if args.check_config:
        return check_config(settings)

    if not settings.telegram_bot_token.is_set:
        print(
            f"{BAD} TELEGRAM_BOT_TOKEN is not set — the bot cannot start.\n"
            "Run `python -m app --check-config` to see everything that is missing.",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE

    # Hand the resolved values to the logger so no library can leak one into a log line.
    known_secrets = [
        value
        for ref in [settings.telegram_bot_token, *(r for p in settings.profiles for r in p.secrets())]
        if (value := ref.resolve_optional())
    ]
    configure_logging(settings.log_level, known_secrets)

    from app.service import run  # imported late so --check-config never needs the bot deps

    asyncio.run(run(settings))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
