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
    # What to do when a receiver re-enters itself for the same object twice.
    "RECURSION": "warn",
    # What to do when a receiver sends email, queues a Celery task or makes an
    # HTTP call inside a transaction that has not committed yet.
    "SIDE_EFFECTS": "warn",
    # Dotted paths of your own functions or methods that count as side
    # effects, e.g. ["shop.webhooks.post", "shop.payments.Client.charge"].
    "SIDE_EFFECT_CALLS": (),
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
    recursion: str
    side_effects: str
    side_effect_calls: tuple[str, ...]
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

    for key in ("BYPASS", "RECURSION", "SIDE_EFFECTS"):
        if merged[key] not in MODES:
            raise ImproperlyConfigured(
                f"SIGNAL_GUARD[{key!r}] must be one of {', '.join(MODES)}, "
                f"not {merged[key]!r}."
            )

    ignore = _as_tuple(merged["IGNORE_RECEIVERS"])
    side_effect_calls = _as_tuple(merged["SIDE_EFFECT_CALLS"])
    first_party = merged["FIRST_PARTY"]

    return Config(
        enabled=bool(merged["ENABLED"]),
        bypass=str(merged["BYPASS"]),
        recursion=str(merged["RECURSION"]),
        side_effects=str(merged["SIDE_EFFECTS"]),
        side_effect_calls=side_effect_calls,
        ignore_receivers=ignore,
        check_library_calls=bool(merged["CHECK_LIBRARY_CALLS"]),
        first_party=None if first_party is None else _as_tuple(first_party),
        lock_file=str(merged["LOCK_FILE"]),
    )


def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(value)  # type: ignore[arg-type]
