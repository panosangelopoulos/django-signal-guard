"""Find and describe the receivers a bulk write would skip."""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, TypeVar

from django.db.models.signals import post_save, pre_save
from django.dispatch import Signal

F = TypeVar("F", bound=Callable[..., Any])

BULK_SAFE_ATTR = "_signal_guard_bulk_safe"

# The signals that Model.save() sends and bulk writes do not.
SAVE_SIGNALS: tuple[tuple[str, Signal], ...] = (
    ("pre_save", pre_save),
    ("post_save", post_save),
)


@dataclass(frozen=True)
class SkippedReceiver:
    signal_name: str
    path: str
    location: str | None


def bulk_safe(func: F) -> F:
    """Mark a receiver as safe to skip during bulk writes.

    Use it for receivers whose work does not matter for bulk writes, such as
    a cache warm-up that a nightly job redoes anyway. Order relative to
    ``@receiver`` does not matter.
    """
    setattr(func, BULK_SAFE_ATTR, True)
    return func


def _is_bulk_safe(receiver: Callable[..., Any]) -> bool:
    # inspect.unwrap: @bulk_safe may sit outside @receiver_on_commit, which
    # connects a wrapper around the function.
    if getattr(receiver, BULK_SAFE_ATTR, False):
        return True
    try:
        return getattr(inspect.unwrap(receiver), BULK_SAFE_ATTR, False)
    except ValueError:  # pragma: no cover - a cycle of __wrapped__
        return False


def live_receivers(signal: Signal, sender: type) -> list[Callable[..., Any]]:
    """Return the receivers that ``signal.send(sender)`` would call now."""
    try:
        sync_receivers, async_receivers = signal._live_receivers(sender)
    except (AttributeError, TypeError, ValueError):  # pragma: no cover
        # Private API changed in a future Django. Fall back to the public
        # check, which cannot name the receivers.
        return [_UnknownReceiver()] if signal.has_listeners(sender) else []
    return [*sync_receivers, *async_receivers]


def skipped_receivers(
    model: type, ignore_patterns: Iterable[str] = ()
) -> tuple[SkippedReceiver, ...]:
    """Return the save receivers for ``model`` that a bulk write would skip."""
    patterns = tuple(ignore_patterns)
    found: list[SkippedReceiver] = []
    for name, signal in SAVE_SIGNALS:
        for receiver in live_receivers(signal, model):
            if _is_bulk_safe(receiver):
                continue
            described = describe(name, receiver)
            if any(fnmatchcase(described.path, p) for p in patterns):
                continue
            found.append(described)
    return tuple(found)


def describe(signal_name: str, receiver: Callable[..., Any]) -> SkippedReceiver:
    target: Any = receiver
    while isinstance(target, functools.partial):
        target = target.func
    target = getattr(target, "__func__", target)  # bound method -> function

    module = getattr(target, "__module__", None) or type(target).__module__
    qualname = getattr(target, "__qualname__", None) or type(target).__qualname__
    path = f"{module}.{qualname}"

    location = None
    try:
        code = inspect.unwrap(target).__code__
    except (AttributeError, ValueError):
        code = None
    if code is not None:
        location = f"{relative_path(code.co_filename)}:{code.co_firstlineno}"

    return SkippedReceiver(signal_name=signal_name, path=path, location=location)


def relative_path(filename: str) -> str:
    try:
        rel = os.path.relpath(filename)
    except ValueError:  # different drive on Windows
        return filename
    return filename if rel.startswith("..") else rel


class _UnknownReceiver:  # pragma: no cover
    __module__ = "<unknown>"
    __qualname__ = "receiver"
