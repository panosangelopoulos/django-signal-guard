"""Find and describe the receivers a bulk write would skip."""

from __future__ import annotations

import dataclasses
import functools
import inspect
import os
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePath
from typing import Any, TypeVar

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import Signal
from django.dispatch.dispatcher import NONE_ID, _make_id

from .heuristics import is_create_only

F = TypeVar("F", bound=Callable[..., Any])

BULK_SAFE_ATTR = "_signal_guard_bulk_safe"

# The signals that Model.save() sends and bulk writes do not.
SAVE_SIGNALS: tuple[tuple[str, Signal], ...] = (
    ("pre_save", pre_save),
    ("post_save", post_save),
)


@dataclass(frozen=True)
class SkippedReceiver:
    """A receiver a bulk write skips, as listed in ``SignalBypassError.receivers``.

    ``path`` is its dotted path, ``location`` the ``file:line`` it is
    defined at, when known.
    """

    signal_name: str
    path: str
    location: str | None
    # The module the receiver is defined in.
    module: str = ""
    # Connected for every sender (sender=None) rather than for this model.
    any_sender: bool = False
    # A post_save receiver that, judging by its source, only acts when
    # created, skipped by a write that creates no rows. A heuristic.
    create_only: bool = False


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
    model: type, ignore_patterns: Iterable[str] = (), *, creates_rows: bool = True
) -> tuple[SkippedReceiver, ...]:
    """Return the save receivers for ``model`` that a bulk write would skip.

    ``creates_rows`` False (``update()``, ``bulk_update()``) also checks each
    post_save receiver for a create-only guard.
    """
    patterns = tuple(ignore_patterns)
    found: list[SkippedReceiver] = []
    for name, signal in SAVE_SIGNALS:
        receivers = live_receivers(signal, model)
        if not receivers:
            continue
        any_sender = _any_sender_ids(signal)
        for receiver in receivers:
            if _is_bulk_safe(receiver):
                continue
            described = describe(name, receiver)
            if any(fnmatchcase(described.path, p) for p in patterns):
                continue
            found.append(
                dataclasses.replace(
                    described,
                    any_sender=_make_id(receiver) in any_sender,
                    create_only=not creates_rows
                    and signal is post_save
                    and is_create_only(receiver),
                )
            )
    return tuple(found)


def _any_sender_ids(signal: Signal) -> set[Any]:
    """The ids of receivers connected to ``signal`` with ``sender=None``."""
    ids = set()
    try:
        for entry in signal.receivers:
            lookup_key, receiver = entry[0], entry[1]
            if lookup_key[1] != NONE_ID:
                continue
            if isinstance(receiver, weakref.ReferenceType):
                receiver = receiver()  # also a WeakMethod
            if receiver is not None:
                ids.add(_make_id(receiver))
    except (IndexError, TypeError):  # pragma: no cover - private API changed
        pass
    return ids


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

    return SkippedReceiver(
        signal_name=signal_name, path=path, location=location, module=module
    )


def relative_path(filename: str) -> str:
    """``filename`` relative to the project, or unchanged if outside it.

    The project is the current folder, else ``settings.BASE_DIR``. A file
    reached by another name for the same folder, such as through a symlink,
    still counts as inside.
    """
    try:
        rel = os.path.relpath(filename)
    except ValueError:  # different drive on Windows
        return filename
    if not rel.startswith(".."):
        return rel
    return _relative_to_project(filename, os.getcwd(), _base_dir())


def _base_dir() -> str | None:
    if not settings.configured:
        return None
    base = getattr(settings, "BASE_DIR", None)
    return os.fspath(base) if base else None


@functools.lru_cache(maxsize=4096)
def _relative_to_project(filename: str, cwd: str, base_dir: str | None) -> str:
    roots = [root for root in (cwd, base_dir) if root]
    for root in roots:
        found = _relative_to(filename, root)
        if found is not None:
            return found
    if os.path.isabs(filename) and not os.path.exists(filename):
        for root in roots:
            found = _moved_into(filename, root)
            if found is not None:
                return found
    return filename


def _moved_into(filename: str, root: str) -> str | None:
    """The file under ``root`` that ``filename``, which does not exist, names.

    Code compiled on another machine keeps that machine's paths. pytest
    before 9.2 reuses its cached rewritten test modules without correcting
    them, so a container that shares the project folder with the host can
    see /Users/me/project/conftest.py. The longest end of the path that is a
    file under ``root`` is taken to be the same file.
    """
    parts = PurePath(filename).parts[1:]  # without the root or drive
    for start in range(len(parts)):
        candidate = os.path.join(*parts[start:])
        if os.path.isfile(os.path.join(root, candidate)):
            return candidate
    return None


def _relative_to(filename: str, root: str) -> str | None:
    try:
        rel = os.path.relpath(os.path.realpath(filename), os.path.realpath(root))
    except ValueError:  # pragma: no cover - different drive on Windows
        return None
    if not rel.startswith(".."):
        return rel
    # The same folder under another spelling, such as different case on a
    # case-insensitive disk.
    head, parts = filename, []
    while True:
        head, tail = os.path.split(head)
        if not tail:
            return None
        parts.append(tail)
        try:
            if os.path.samefile(head, root):
                return os.path.join(*reversed(parts))
        except OSError:
            continue


class _UnknownReceiver:  # pragma: no cover
    __module__ = "<unknown>"
    __qualname__ = "receiver"
