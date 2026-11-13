"""The bypass guard.

``QuerySet.bulk_create()``, ``bulk_update()`` and ``update()`` write rows
without calling ``Model.save()``, so ``pre_save`` and ``post_save`` receivers
never run. The guard wraps those three methods and, before the write, reports
any live receivers that would be skipped.
"""

from __future__ import annotations

import functools
import logging
import sys
import warnings
from collections.abc import Callable
from contextlib import ContextDecorator
from contextvars import ContextVar
from dataclasses import dataclass
from types import FrameType
from typing import Any

from django.db.models import Model
from django.db.models.query import QuerySet
from django.db.models.signals import post_save, pre_save

from .conf import get_config
from .exceptions import SignalBypassError, SignalBypassWarning
from .inspection import is_first_party
from .receivers import SkippedReceiver, relative_path, skipped_receivers

logger = logging.getLogger("signal_guard")

GUARDED_METHODS = ("bulk_create", "bulk_update", "update")
ORIGINAL_ATTR = "__signal_guard_original__"

# Frames that only forward a call: the manager proxy that turns
# Order.objects.update() into a QuerySet call, this package, and the
# machinery that runs sync code for abulk_create() and friends.
_PASS_THROUGH_MODULES = (
    "signal_guard",
    "django.db.models.manager",
    "asgiref",
    "concurrent",
    "threading",
)

# True while a guarded method is running. bulk_update() calls update()
# internally, and that inner call must not be reported a second time.
_inside_guarded_call: ContextVar[bool] = ContextVar(
    "signal_guard_inside_guarded_call", default=False
)
_allowances: ContextVar[tuple[allow_bypass, ...]] = ContextVar(
    "signal_guard_allowances", default=()
)


class allow_bypass(ContextDecorator):
    """Allow bulk writes that skip receivers, and record why.

    ::

        with allow_bypass(Order, reason="import reindexes in one batch"):
            Order.objects.bulk_create(rows)

    With no models it allows every model. Works as a decorator on sync
    functions and as ``async with`` in async code.
    """

    def __init__(self, *models: type[Model], reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                "allow_bypass() needs a non-empty reason, for example "
                'reason="receivers are run in bulk by reindex_orders()".'
            )
        for model in models:
            if not (isinstance(model, type) and issubclass(model, Model)):
                raise TypeError(f"allow_bypass() takes model classes, not {model!r}.")
        self.models: tuple[type[Model], ...] = models
        self.reason = reason

    def covers(self, model: type[Model]) -> bool:
        if not self.models:
            return True
        return any(issubclass(model, allowed) for allowed in self.models)

    # State lives only in the context variable, never on the instance, so one
    # instance can be entered from several threads or tasks at once (as a
    # decorator on a view, say) without them interfering.
    def __enter__(self) -> allow_bypass:
        _allowances.set((*_allowances.get(), self))
        return self

    def __exit__(self, *exc_info: object) -> None:
        current = _allowances.get()
        for index in range(len(current) - 1, -1, -1):
            if current[index] is self:
                _allowances.set(current[:index] + current[index + 1 :])
                return

    async def __aenter__(self) -> allow_bypass:
        return self.__enter__()

    async def __aexit__(self, *exc_info: object) -> None:
        self.__exit__(*exc_info)

    def __repr__(self) -> str:
        names = ", ".join(m._meta.label for m in self.models) or "all models"
        return f"<allow_bypass {names}: {self.reason!r}>"


def is_allowed(model: type[Model]) -> bool:
    return any(allowance.covers(model) for allowance in _allowances.get())


@dataclass(frozen=True)
class CallSite:
    filename: str | None
    lineno: int | None
    # True when the call comes from Django or an installed package.
    internal: bool
    stacklevel: int


def _module_matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _find_call_site(start: FrameType | None) -> CallSite:
    """Walk out from ``start`` to the code that asked for the bulk write.

    ``stacklevel`` counts ``start`` as 1, matching ``warnings.warn`` and
    ``logging`` when they are called from the function that owns ``start``.
    """
    frame, depth = start, 1
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not _module_matches(module, _PASS_THROUGH_MODULES):
            return CallSite(
                filename=frame.f_code.co_filename,
                lineno=frame.f_lineno,
                internal=not is_first_party(module),
                stacklevel=depth,
            )
        frame, depth = frame.f_back, depth + 1
    # Only forwarding frames, for example a sync_to_async worker thread.
    return CallSite(filename=None, lineno=None, internal=False, stacklevel=1)


def format_message(
    model: type[Model],
    method: str,
    skipped: tuple[SkippedReceiver, ...],
    site: CallSite,
) -> str:
    count = len(skipped)
    noun = "receiver" if count == 1 else "receivers"
    width = max(len(r.signal_name) for r in skipped)
    lines = [f"QuerySet.{method}() on {model._meta.label} skips {count} {noun}:"]
    for r in skipped:
        where = f"  ({r.location})" if r.location else ""
        lines.append(f"  {r.signal_name.ljust(width)}  {r.path}{where}")
    if site.filename is not None:
        lines.append(f"Called from {relative_path(site.filename)}:{site.lineno}.")
    lines.append(
        "Run the receivers' work yourself, or wrap the call in "
        f'signal_guard.allow_bypass({model.__name__}, reason="...").'
    )
    return "\n".join(lines)


def check_bypass(model: type[Model], method: str) -> None:
    """Report the receivers that ``method`` would skip for ``model``."""
    # Fast path: most models have no save receivers at all.
    if not (pre_save.has_listeners(model) or post_save.has_listeners(model)):
        return

    config = get_config()
    if not config.enabled or config.bypass == "off" or is_allowed(model):
        return

    skipped = skipped_receivers(model, config.ignore_receivers)
    if not skipped:
        return

    site = _find_call_site(sys._getframe(0))
    if site.internal and not config.check_library_calls:
        return

    message = format_message(model, method, skipped, site)
    if config.bypass == "raise":
        raise SignalBypassError(message, model=model, method=method, receivers=skipped)
    if config.bypass == "warn":
        warnings.warn(message, SignalBypassWarning, stacklevel=site.stacklevel)
        return
    logger.warning(
        message,
        stacklevel=site.stacklevel,
        extra={
            "signal_guard_model": model._meta.label,
            "signal_guard_method": method,
            "signal_guard_receivers": [r.path for r in skipped],
        },
    )


def _make_guarded(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def guarded(self: QuerySet, *args: Any, **kwargs: Any) -> Any:
        if _inside_guarded_call.get():
            return original(self, *args, **kwargs)
        token = _inside_guarded_call.set(True)
        try:
            check_bypass(self.model, name)
            return original(self, *args, **kwargs)
        finally:
            _inside_guarded_call.reset(token)

    setattr(guarded, ORIGINAL_ATTR, original)
    return guarded


def install() -> None:
    """Wrap the QuerySet bulk methods. Safe to call more than once."""
    for name in GUARDED_METHODS:
        current = getattr(QuerySet, name)
        if not hasattr(current, ORIGINAL_ATTR):
            setattr(QuerySet, name, _make_guarded(name, current))


def uninstall() -> None:
    """Restore the original QuerySet methods."""
    for name in GUARDED_METHODS:
        current = getattr(QuerySet, name)
        original = getattr(current, ORIGINAL_ATTR, None)
        if original is not None:
            setattr(QuerySet, name, original)


def is_installed() -> bool:
    return all(hasattr(getattr(QuerySet, n), ORIGINAL_ATTR) for n in GUARDED_METHODS)
