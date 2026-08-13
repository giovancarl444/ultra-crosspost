"""Configuration loading: secrets from the environment, everything else from profiles.yaml.

Secrets are referenced *indirectly*. profiles.yaml names the environment variable that
holds a credential (``webhook_env: DISCORD_WEBHOOK_BRAND_A``) and never the credential
itself, so the file is safe to commit. A `SecretRef` reads os.environ on demand and is
never stored, logged, or repr'd with its value attached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_PROFILES_FILE = Path("profiles.yaml")
DEFAULT_POLL_INTERVAL_SECONDS = 120
DEFAULT_LATER_COOLDOWN_MINUTES = 60
DEFAULT_MAX_OFFERS_PER_CYCLE = 5

PLATFORMS = ("reddit", "discord", "x")
"""Platforms that publish a post."""


class ConfigError(Exception):
    """Raised when the configuration is unusable. Carries every problem found, not just
    the first, so one run of --check-config shows the whole list."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"{len(problems)} configuration problem(s)")


@dataclass(frozen=True)
class SecretRef:
    """A credential held in an environment variable named by profiles.yaml."""

    env_var: str
    purpose: str

    @property
    def is_set(self) -> bool:
        return bool(os.environ.get(self.env_var, "").strip())

    def resolve(self) -> str:
        value = os.environ.get(self.env_var, "").strip()
        if not value:
            raise ConfigError([f"{self.env_var} is not set ({self.purpose})"])
        return value

    def resolve_optional(self) -> str | None:
        return os.environ.get(self.env_var, "").strip() or None


@dataclass(frozen=True)
class DriveFolders:
    inbox_folder_id: str
    posted_folder_id: str
    rejected_folder_id: str


@dataclass(frozen=True)
class RedditConfig:
    enabled: bool
    subreddit: str
    nsfw: bool
    flair_text: str | None
    flair_id: str | None
    client_id: SecretRef | None
    client_secret: SecretRef | None
    username: SecretRef | None
    password: SecretRef | None

    def secrets(self) -> list[SecretRef]:
        return [s for s in (self.client_id, self.client_secret, self.username, self.password) if s]


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool
    webhook: SecretRef | None

    def secrets(self) -> list[SecretRef]:
        return [self.webhook] if self.webhook else []


@dataclass(frozen=True)
class XConfig:
    """X is deferred — it has had no free tier since Feb 2026 (see README). The block
    exists so the profile schema is stable when it is switched back on."""

    enabled: bool = False

    def secrets(self) -> list[SecretRef]:
        return []


@dataclass(frozen=True)
class Profile:
    name: str
    telegram_chat_id: int
    drive: DriveFolders
    reddit: RedditConfig
    discord: DiscordConfig
    x: XConfig

    @property
    def enabled_platforms(self) -> tuple[str, ...]:
        return tuple(p for p in PLATFORMS if getattr(self, p).enabled)

    def secrets(self) -> list[SecretRef]:
        refs: list[SecretRef] = []
        for section in (self.reddit, self.discord, self.x):
            if section.enabled:
                refs.extend(section.secrets())
        return refs


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    log_level: str
    poll_interval_seconds: int
    later_cooldown_minutes: int
    max_offers_per_cycle: int
    media_dir: Path
    database_path: Path
    profiles_file: Path
    telegram_bot_token: SecretRef
    google_service_account_file: Path | None
    profiles: tuple[Profile, ...] = field(default_factory=tuple)

    def missing_secrets(self) -> list[SecretRef]:
        """Every referenced-but-unset secret, in declaration order, deduplicated by env var."""
        seen: set[str] = set()
        missing: list[SecretRef] = []
        for ref in [self.telegram_bot_token, *(r for p in self.profiles for r in p.secrets())]:
            if not ref.is_set and ref.env_var not in seen:
                seen.add(ref.env_var)
                missing.append(ref)
        return missing

    def missing_files(self) -> list[str]:
        """Non-secret prerequisites that are named but absent from disk."""
        problems = []
        if self.google_service_account_file is None:
            problems.append(
                "GOOGLE_SERVICE_ACCOUNT_FILE is not set (path to the Drive service-account JSON)"
            )
        elif not self.google_service_account_file.is_file():
            problems.append(
                f"GOOGLE_SERVICE_ACCOUNT_FILE points at {self.google_service_account_file}, "
                "which does not exist"
            )
        return problems

    @property
    def is_complete(self) -> bool:
        return not self.missing_secrets() and not self.missing_files()


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


class _Parser:
    """Collects every problem with a precise path rather than failing on the first one."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def fail(self, path: str, message: str) -> None:
        self.problems.append(f"{path}: {message}")

    def section(self, data: Any, key: str, path: str) -> dict[str, Any]:
        value = data.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.fail(f"{path}.{key}", f"expected a mapping, got {type(value).__name__}")
            return {}
        return value

    def string(self, data: Any, key: str, path: str, *, required: bool = True) -> str | None:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                self.fail(f"{path}.{key}", "is required")
            return None
        if not isinstance(value, str):
            self.fail(f"{path}.{key}", f"expected a string, got {type(value).__name__}")
            return None
        return value.strip()

    def integer(self, data: Any, key: str, path: str, *, default: int | None = None) -> int | None:
        value = data.get(key, default)
        if value is None:
            self.fail(f"{path}.{key}", "is required")
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(f"{path}.{key}", f"expected an integer, got {type(value).__name__}")
            return None
        return value

    def boolean(self, data: Any, key: str, path: str, *, default: bool) -> bool:
        value = data.get(key, default)
        if not isinstance(value, bool):
            self.fail(f"{path}.{key}", f"expected true or false, got {type(value).__name__}")
            return default
        return value

    def string_list(self, data: Any, key: str, path: str) -> tuple[str, ...]:
        value = data.get(key) or []
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            self.fail(f"{path}.{key}", "expected a list of strings")
            return ()
        return tuple(v.strip() for v in value if v.strip())

    def secret(
        self, data: Any, key: str, path: str, purpose: str, *, required: bool
    ) -> SecretRef | None:
        env_var = self.string(data, key, path, required=required)
        return SecretRef(env_var=env_var, purpose=purpose) if env_var else None


def _parse_drive(parser: _Parser, data: dict[str, Any], path: str) -> DriveFolders:
    folders = {
        key: parser.string(data, key, path)
        for key in ("inbox_folder_id", "posted_folder_id", "rejected_folder_id")
    }
    present = [v for v in folders.values() if v]
    if len(present) != len(set(present)):
        parser.fail(path, "inbox, posted and rejected must be three different folders")
    return DriveFolders(
        inbox_folder_id=folders["inbox_folder_id"] or "",
        posted_folder_id=folders["posted_folder_id"] or "",
        rejected_folder_id=folders["rejected_folder_id"] or "",
    )


def _parse_reddit(parser: _Parser, data: dict[str, Any], path: str, profile: str) -> RedditConfig:
    enabled = parser.boolean(data, "enabled", path, default=False)
    subreddit = parser.string(data, "subreddit", path, required=enabled)
    if subreddit:
        subreddit = subreddit.removeprefix("/r/").removeprefix("r/")
    return RedditConfig(
        enabled=enabled,
        subreddit=subreddit or "",
        nsfw=parser.boolean(data, "nsfw", path, default=False),
        flair_text=parser.string(data, "flair_text", path, required=False),
        flair_id=parser.string(data, "flair_id", path, required=False),
        client_id=parser.secret(
            data, "client_id_env", path, f"Reddit app client id for {profile}", required=enabled
        ),
        client_secret=parser.secret(
            data, "client_secret_env", path, f"Reddit app secret for {profile}", required=enabled
        ),
        username=parser.secret(
            data, "username_env", path, f"Reddit account for {profile}", required=enabled
        ),
        password=parser.secret(
            data, "password_env", path, f"Reddit password for {profile}", required=enabled
        ),
    )


def _parse_profile(parser: _Parser, data: Any, index: int) -> Profile | None:
    path = f"profiles[{index}]"
    if not isinstance(data, dict):
        parser.fail(path, f"expected a mapping, got {type(data).__name__}")
        return None

    name = parser.string(data, "name", path)
    chat_id = parser.integer(data, "telegram_chat_id", path)
    label = name or path

    discord_data = parser.section(data, "discord", path)
    discord_enabled = parser.boolean(discord_data, "enabled", f"{path}.discord", default=False)

    x_enabled = parser.boolean(parser.section(data, "x", path), "enabled", f"{path}.x", default=False)
    if x_enabled:
        parser.fail(
            f"{path}.x.enabled",
            "X is not implemented in this build — it has had no free tier since Feb 2026. "
            "See the README section 'X (deferred)'.",
        )

    profile = Profile(
        name=name or "",
        telegram_chat_id=chat_id or 0,
        drive=_parse_drive(parser, parser.section(data, "drive", path), f"{path}.drive"),
        reddit=_parse_reddit(parser, parser.section(data, "reddit", path), f"{path}.reddit", label),
        discord=DiscordConfig(
            enabled=discord_enabled,
            webhook=parser.secret(
                discord_data,
                "webhook_env",
                f"{path}.discord",
                f"Discord webhook URL for {label}",
                required=discord_enabled,
            ),
        ),
        x=XConfig(enabled=x_enabled),
    )

    if not profile.enabled_platforms:
        parser.fail(path, f"profile '{label}' has no enabled platforms — it would never post")
    return profile


def _parse_profiles_file(parser: _Parser, path: Path) -> tuple[dict[str, Any], list[Profile]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        parser.fail(str(path), "file not found")
        return {}, []
    except yaml.YAMLError as exc:
        parser.fail(str(path), f"is not valid YAML: {exc}")
        return {}, []

    if not isinstance(raw, dict):
        parser.fail(str(path), "expected a mapping at the top level")
        return {}, []

    entries = raw.get("profiles")
    if not isinstance(entries, list) or not entries:
        parser.fail("profiles", "expected a non-empty list of profiles")
        return raw, []

    profiles = [p for i, e in enumerate(entries) if (p := _parse_profile(parser, e, i))]

    for label, values in (
        ("name", [p.name for p in profiles]),
        ("telegram_chat_id", [p.telegram_chat_id for p in profiles]),
    ):
        duplicates = {v for v in values if values.count(v) > 1}
        for dupe in sorted(duplicates, key=str):
            parser.fail("profiles", f"duplicate {label}: {dupe}")

    return raw, profiles


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(profiles_file: Path | None = None, *, load_env: bool = True) -> Settings:
    """Load .env then profiles.yaml. Raises ConfigError listing every problem found.

    A missing *credential* is not a problem here — that is reported separately by
    `Settings.missing_secrets()` so --check-config can show the config even when the
    secrets have not been obtained yet.
    """
    if load_env:
        load_dotenv(override=False)

    path = profiles_file or Path(os.environ.get("PROFILES_FILE", DEFAULT_PROFILES_FILE))
    parser = _Parser()
    raw, profiles = _parse_profiles_file(parser, path)

    poll_interval = parser.integer(
        raw, "poll_interval_seconds", "config", default=DEFAULT_POLL_INTERVAL_SECONDS
    )
    if poll_interval is not None and poll_interval < 10:
        parser.fail("config.poll_interval_seconds", "must be at least 10 seconds")

    later_cooldown = parser.integer(
        raw, "later_cooldown_minutes", "config", default=DEFAULT_LATER_COOLDOWN_MINUTES
    )
    max_offers = parser.integer(
        raw, "max_offers_per_cycle", "config", default=DEFAULT_MAX_OFFERS_PER_CYCLE
    )
    if max_offers is not None and max_offers < 1:
        parser.fail("config.max_offers_per_cycle", "must be at least 1")

    if parser.problems:
        raise ConfigError(parser.problems)

    service_account = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

    return Settings(
        dry_run=_env_flag("DRY_RUN", default=True),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        poll_interval_seconds=poll_interval or DEFAULT_POLL_INTERVAL_SECONDS,
        later_cooldown_minutes=later_cooldown or DEFAULT_LATER_COOLDOWN_MINUTES,
        max_offers_per_cycle=max_offers or DEFAULT_MAX_OFFERS_PER_CYCLE,
        media_dir=Path(str(raw.get("media_dir", "media"))),
        database_path=Path(str(raw.get("database_path", "crosspost.db"))),
        profiles_file=path,
        telegram_bot_token=SecretRef("TELEGRAM_BOT_TOKEN", "BotFather token for the bot"),
        google_service_account_file=Path(service_account) if service_account else None,
        profiles=tuple(profiles),
    )
