"""The bypass guard.

``QuerySet.bulk_create()``, ``bulk_update()`` and ``update()`` write rows
without calling ``Model.save()``, so ``pre_save`` and ``post_save`` receivers
never run. The guard wraps those three methods and, before the write, reports
any live receivers that would be skipped.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from contextvars import ContextVar
from types import FrameType
from typing import Any

from django.db.models import Model
from django.db.models.manager import BaseManager
from django.db.models.query import QuerySet
from django.db.models.signals import post_save, pre_save

from . import tracing
from .allowance import Allowance
from .conf import Config, get_config
from .exceptions import SignalBypassError, SignalBypassWarning
from .inspection import is_first_party
from .receivers import SkippedReceiver, skipped_receivers
from .reporting import CallSite, emit, find_call_site

GUARDED_METHODS = ("bulk_create", "bulk_update", "update")
ORIGINAL_ATTR = "__signal_guard_original__"

# The module Django gives the historical models that migrations use.
HISTORICAL_MODULE = "__fake__"


# True while a guarded method is running. bulk_update() calls update()
# internally, and that inner call must not be reported a second time.
_inside_guarded_call: ContextVar[bool] = ContextVar(
    "signal_guard_inside_guarded_call", default=False
)


class allow_bypass(Allowance):
    """Allow bulk writes that skip receivers, and record why.

    ::

        with allow_bypass(Order, reason="import reindexes in one batch"):
            Order.objects.bulk_create(rows)

    With no models it allows every model. Works as a decorator on sync
    functions and as ``async with`` in async code.
    """

    active = ContextVar("signal_guard_allowances", default=())

    def __init__(self, *models: type[Model], reason: str) -> None:
        super().__init__(
            reason=reason, example="receivers are run in bulk by reindex_orders()"
        )
        for model in models:
            if not (isinstance(model, type) and issubclass(model, Model)):
                raise TypeError(f"allow_bypass() takes model classes, not {model!r}.")
        self.models: tuple[type[Model], ...] = models

    def covers(self, model: type[Model]) -> bool:
        if not self.models:
            return True
        return any(issubclass(model, allowed) for allowed in self.models)

    def __repr__(self) -> str:
        names = ", ".join(m._meta.label for m in self.models) or "all models"
        return f"<allow_bypass {names}: {self.reason!r}>"


def is_allowed(model: type[Model]) -> bool:
    return any(allowance.covers(model) for allowance in allow_bypass.entered())


def _write_override(frame: FrameType) -> str | None:
    """Name a project's or library's override of a guarded method, if it is one.

    A manager or queryset that overrides update() (to set updated_at, say) and
    calls super() would otherwise be blamed for every update() in the project.
    """
    name = frame.f_code.co_name
    if name not in GUARDED_METHODS or not frame.f_code.co_argcount:
        return None
    self = frame.f_locals.get("self")
    if isinstance(self, QuerySet | BaseManager):
        return f"{type(self).__name__}.{name}()"
    return None


def _is_reported(receiver: SkippedReceiver, config: Config) -> bool:
    if config.ignore_create_only_receivers and receiver.create_only:
        return False
    if config.library_receivers == "report" or is_first_party(receiver.module):
        return True
    return config.library_receivers == "ignore_global" and not receiver.any_sender


def format_message(
    model: type[Model],
    method: str,
    skipped: tuple[SkippedReceiver, ...],
    site: CallSite,
    *,
    inside: str | None = None,
    running: tracing.RunningReceiver | None = None,
) -> str:
    count = len(skipped)
    noun = "receiver" if count == 1 else "receivers"
    width = max(len(r.signal_name) for r in skipped)
    lines = [f"QuerySet.{method}() on {model._meta.label} skips {count} {noun}:"]
    for r in skipped:
        where = f"  ({r.location})" if r.location else ""
        flag = "  [create-only?]" if r.create_only else ""
        lines.append(f"  {r.signal_name.ljust(width)}  {r.path}{where}{flag}")
    if site.location is not None:
        through = f", through {site.through}" if site.through else ""
        lines.append(f"Called from {site.location}{through}.")
    if inside is not None:
        who = running.path if running is not None else "a receiver"
        lines.append(
            f"This {method}() runs inside {who}, a {inside} receiver for "
            f"{model._meta.label}, which usually writes this way on purpose so "
            "it does not run itself again. Set IGNORE_RECEIVER_SELF_UPDATES "
            "to stop reporting these."
        )
    create_only = sum(r.create_only for r in skipped)
    if create_only:
        which = (
            "The receiver marked [create-only?] seems"
            if create_only == 1
            else f"The {create_only} receivers marked [create-only?] seem"
        )
        lines.append(
            f"{which} to act only when created, judging by the source, and "
            f"{method}() creates no rows. Set IGNORE_CREATE_ONLY_RECEIVERS to "
            "skip such receivers."
        )
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
    if config.ignore_historical_models and model.__module__ == HISTORICAL_MODULE:
        return

    skipped = tuple(
        r
        for r in skipped_receivers(
            model, config.ignore_receivers, creates_rows=method == "bulk_create"
        )
        if _is_reported(r, config)
    )
    if not skipped:
        return

    site = find_call_site(sys._getframe(0), skip=_write_override)
    if site.internal and not config.check_library_calls:
        return

    inside = tracing.save_dispatch_for(model)
    if inside is not None and config.ignore_receiver_self_updates:
        return
    running = tracing.current_receiver() if inside is not None else None

    message = format_message(
        model, method, skipped, site, inside=inside, running=running
    )
    emit(
        config.bypass,
        message,
        kind="bypass",
        location=site.location,
        error=lambda: SignalBypassError(
            message, model=model, method=method, receivers=skipped
        ),
        warning=SignalBypassWarning,
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
