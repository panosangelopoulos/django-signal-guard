"""Settings for django-signal-guard, read from ``settings.SIGNAL_GUARD``."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

MODES = ("raise", "warn", "log", "off")

DEFAULTS: dict[str, object] = {
    # Install the guard at all. When False the QuerySet methods are left alone.
    "ENABLED": True,
    # What to do when a bulk write would skip receivers.
    "BYPASS": "warn",
    # fnmatch patterns matched against receiver paths such as
    # "simple_history.models.HistoricalRecords.post_save".
    "IGNORE_RECEIVERS": (),
    # Also report bulk writes made inside Django or installed packages, such as
    # the UPDATE behind on_delete=SET_NULL. You usually cannot change that code.
    "CHECK_LIBRARY_CALLS": False,
    # Module prefixes of your own code, e.g. ["shop", "billing"]. By default,
    # anything not installed in site-packages and not Django is first-party.
    "FIRST_PARTY": None,
    # Where signal_map --write and --check keep the lock file. A relative path
    # is resolved against settings.BASE_DIR when set, else the current folder.
    "LOCK_FILE": "signals.lock",
}


@dataclass(frozen=True)
class Config:
    enabled: bool
    bypass: str
    ignore_receivers: tuple[str, ...]
    check_library_calls: bool
    first_party: tuple[str, ...] | None
    lock_file: str


def get_config() -> Config:
    """Return the current configuration.

    Settings are read on every call so ``override_settings`` works in tests.
    """
    user = getattr(settings, "SIGNAL_GUARD", None) or {}
    if not isinstance(user, dict):
        raise ImproperlyConfigured("SIGNAL_GUARD must be a dict.")

    unknown = sorted(set(user) - set(DEFAULTS))
    if unknown:
        raise ImproperlyConfigured(
            f"Unknown SIGNAL_GUARD key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(DEFAULTS)}."
        )

    merged = {**DEFAULTS, **user}

    bypass = merged["BYPASS"]
    if bypass not in MODES:
        raise ImproperlyConfigured(
            f"SIGNAL_GUARD['BYPASS'] must be one of {', '.join(MODES)}, not {bypass!r}."
        )

    ignore = merged["IGNORE_RECEIVERS"]
    if isinstance(ignore, str):
        ignore = (ignore,)

    first_party = merged["FIRST_PARTY"]
    if isinstance(first_party, str):
        first_party = (first_party,)

    return Config(
        enabled=bool(merged["ENABLED"]),
        bypass=str(bypass),
        ignore_receivers=tuple(ignore),
        check_library_calls=bool(merged["CHECK_LIBRARY_CALLS"]),
        first_party=None if first_party is None else tuple(first_party),
        lock_file=str(merged["LOCK_FILE"]),
    )
