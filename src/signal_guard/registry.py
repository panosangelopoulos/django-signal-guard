"""Record every ``Signal.connect()`` and ``disconnect()``.

Django's dispatcher forgets a receiver as soon as it is garbage-collected or
disconnected, and never records where or when it was connected. The registry
keeps that history so the system checks can explain what went wrong:

* a weak receiver that died and silently stopped running,
* a receiver connected only after startup, by a lazy import,
* the file and line that connected each receiver.

It holds no strong reference to receivers, so it never keeps one alive.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import sys
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from django.apps import apps
from django.dispatch import Signal
from django.dispatch.dispatcher import _make_id

from .receivers import describe, relative_path

ORIGINAL_ATTR = "__signal_guard_original__"

# Frames that only forward a connect() call.
_FORWARDING_MODULES = (
    "signal_guard",
    "django.dispatch",
    "django.db.models.signals",
)
# Frames that only run an import.
_IMPORT_MACHINERY = ("importlib",)


@dataclass
class ConnectRecord:
    signal_ref: weakref.ReferenceType[Signal]
    receiver_ref: Callable[[], Any] | None
    receiver_module: str
    receiver_qualname: str
    receiver_path: str
    receiver_file: str | None
    is_bound_method: bool
    lookup_key: tuple[Any, Any]
    sender_key: Any
    sender_label: str
    weak: bool
    dispatch_uid: Any
    # Where connect() was called: the @receiver line or the .connect() line.
    connected_at: str | None
    # True when the app registry was already ready, i.e. after every
    # AppConfig.ready() had run.
    late: bool
    # For late connects, the first frame outside the receiver's own file:
    # usually the import that pulled the receiver module in.
    triggered_by: str | None
    removed: bool = field(default=False)

    @property
    def signal(self) -> Signal | None:
        return self.signal_ref()

    @property
    def is_dead(self) -> bool:
        return self.receiver_ref is not None and self.receiver_ref() is None


RECORDS: list[ConnectRecord] = []


def _module_matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _site(frame: FrameType) -> str:
    return f"{relative_path(frame.f_code.co_filename)}:{frame.f_lineno}"


def _caller_sites(receiver_file: str | None) -> tuple[str | None, str | None]:
    """Return (connected_at, triggered_by) for the current connect() call."""
    frame: FrameType | None = sys._getframe(2)
    connected_at = None
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not _module_matches(module, _FORWARDING_MODULES):
            connected_at = _site(frame)
            break
        frame = frame.f_back

    triggered_by = None
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        skip = _module_matches(module, _FORWARDING_MODULES + _IMPORT_MACHINERY)
        if not skip and frame.f_code.co_filename != receiver_file:
            triggered_by = _site(frame)
            break
        frame = frame.f_back
    return connected_at, triggered_by


def _weak_handle(receiver: Any) -> Callable[[], Any] | None:
    try:
        if hasattr(receiver, "__self__") and hasattr(receiver, "__func__"):
            return weakref.WeakMethod(receiver)
        return weakref.ref(receiver)
    except TypeError:
        return None


def sender_label(sender: Any) -> str:
    if sender is None:
        return "*"
    meta = getattr(sender, "_meta", None)
    if isinstance(sender, type) and meta is not None:
        return meta.label
    label = getattr(sender, "label", None)
    if label is not None and hasattr(sender, "models_module"):
        return f"app:{label}"  # an AppConfig, the sender of post_migrate
    if isinstance(sender, type):
        return f"{sender.__module__}.{sender.__qualname__}"
    return f"<{type(sender).__module__}.{type(sender).__qualname__} instance>"


def _was_added(signal: Signal, lookup_key: Any, receiver: Any) -> bool:
    """True if connect() stored ``receiver`` rather than ignoring a duplicate."""
    for entry in signal.receivers:
        if entry[0] != lookup_key:
            continue
        stored = entry[1]
        if isinstance(stored, weakref.ReferenceType):
            stored = stored()
        return stored == receiver
    return False


def _record(
    signal: Signal, receiver: Any, sender: Any, weak: bool, dispatch_uid: Any
) -> None:
    if dispatch_uid:
        lookup_key = (dispatch_uid, _make_id(sender))
    else:
        lookup_key = (_make_id(receiver), _make_id(sender))
    if not _was_added(signal, lookup_key, receiver):
        return

    described = describe("", receiver)
    target = getattr(receiver, "__func__", receiver)
    module = getattr(target, "__module__", None) or type(target).__module__
    qualname = getattr(target, "__qualname__", None) or type(target).__qualname__
    with contextlib.suppress(ValueError):
        target = inspect.unwrap(target)  # @receiver_on_commit's wrapper
    code = getattr(target, "__code__", None)
    receiver_file = code.co_filename if code is not None else None
    connected_at, triggered_by = _caller_sites(receiver_file)
    late = apps.ready

    RECORDS.append(
        ConnectRecord(
            signal_ref=weakref.ref(signal),
            receiver_ref=_weak_handle(receiver),
            receiver_module=module,
            receiver_qualname=qualname,
            receiver_path=described.path,
            receiver_file=receiver_file,
            is_bound_method=hasattr(receiver, "__self__")
            and hasattr(receiver, "__func__"),
            lookup_key=lookup_key,
            sender_key=_make_id(sender),
            sender_label=sender_label(sender),
            weak=weak,
            dispatch_uid=dispatch_uid,
            connected_at=connected_at,
            late=late,
            triggered_by=triggered_by if late else None,
        )
    )


def _mark_removed(signal: Signal, receiver: Any, sender: Any, uid: Any) -> None:
    if uid:
        lookup_key = (uid, _make_id(sender))
    else:
        lookup_key = (_make_id(receiver), _make_id(sender))
    for record in RECORDS:
        if record.signal is signal and record.lookup_key == lookup_key:
            record.removed = True


def _wrap_connect(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def connect(self, receiver, sender=None, weak=True, dispatch_uid=None):
        result = original(
            self, receiver, sender=sender, weak=weak, dispatch_uid=dispatch_uid
        )
        # Recording must never break connect().
        with contextlib.suppress(Exception):
            _record(self, receiver, sender, weak, dispatch_uid)
        return result

    setattr(connect, ORIGINAL_ATTR, original)
    return connect


def _wrap_disconnect(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def disconnect(self, receiver=None, sender=None, dispatch_uid=None):
        result = original(
            self, receiver=receiver, sender=sender, dispatch_uid=dispatch_uid
        )
        if result:
            _mark_removed(self, receiver, sender, dispatch_uid)
        return result

    setattr(disconnect, ORIGINAL_ATTR, original)
    return disconnect


def install() -> None:
    if not hasattr(Signal.connect, ORIGINAL_ATTR):
        Signal.connect = _wrap_connect(Signal.connect)
    if not hasattr(Signal.disconnect, ORIGINAL_ATTR):
        Signal.disconnect = _wrap_disconnect(Signal.disconnect)


def uninstall() -> None:
    for name in ("connect", "disconnect"):
        original = getattr(getattr(Signal, name), ORIGINAL_ATTR, None)
        if original is not None:
            setattr(Signal, name, original)


def is_installed() -> bool:
    return hasattr(Signal.connect, ORIGINAL_ATTR)
