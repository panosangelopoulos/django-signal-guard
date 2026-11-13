"""Find the code to blame for a problem, and report it in the configured mode."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any

from .inspection import is_first_party

logger = logging.getLogger("signal_guard")

# Frames that only forward a call: this package, the manager proxy that turns
# Order.objects.update() into a QuerySet call, and the machinery that runs
# sync code for abulk_create() and friends.
PASS_THROUGH_MODULES = (
    "signal_guard",
    "django.db.models.manager",
    "asgiref",
    "concurrent",
    "threading",
)


@dataclass(frozen=True)
class CallSite:
    filename: str | None
    lineno: int | None
    # True when the call comes from Django or an installed package.
    internal: bool
    stacklevel: int


def module_matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def find_call_site(
    start: FrameType | None,
    pass_through: tuple[str, ...] = PASS_THROUGH_MODULES,
) -> CallSite:
    """Walk out from ``start`` to the first frame that is not pass-through.

    ``stacklevel`` counts ``start`` as 1, matching ``warnings.warn`` and
    ``logging`` when they are called from the function that owns ``start``.
    """
    frame, depth = start, 1
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not module_matches(module, pass_through):
            return CallSite(
                filename=frame.f_code.co_filename,
                lineno=frame.f_lineno,
                internal=not is_first_party(module),
                stacklevel=depth,
            )
        frame, depth = frame.f_back, depth + 1
    # Only forwarding frames, for example a sync_to_async worker thread.
    return CallSite(filename=None, lineno=None, internal=False, stacklevel=1)


def emit(
    mode: str,
    message: str,
    *,
    error: Callable[[], Exception],
    warning: type[Warning],
    stacklevel: int,
    extra: dict[str, Any] | None = None,
) -> None:
    """Raise, warn or log ``message`` according to ``mode``.

    ``stacklevel`` is relative to the caller of ``emit``.
    """
    if mode == "raise":
        raise error()
    if mode == "warn":
        warnings.warn(message, warning, stacklevel=stacklevel + 1)
    elif mode == "log":
        logger.warning(message, stacklevel=stacklevel + 1, extra=extra or {})
