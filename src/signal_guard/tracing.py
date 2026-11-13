"""Trace signal dispatch: which signals are being sent right now, and by whom.

``Signal.send()`` and friends are wrapped so that each dispatch pushes an entry
onto a per-context stack. Two features read that stack:

* recursion tracing: a signal sent again for the same object while it is
  already being dispatched twice is a loop, usually a ``post_save`` receiver
  that calls ``instance.save()``;
* the side-effect check, which needs to know which receiver is running.

Receivers are found by walking Python frames between two dispatches, only
when something is reported, so the cost of an ordinary send is one context
variable push and pop.
"""

from __future__ import annotations

import functools
import gc
import sys
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from types import FrameType
from typing import Any

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import Signal
from django.dispatch.dispatcher import _make_id

from .conf import get_config
from .exceptions import SignalRecursionError, SignalRecursionWarning
from .reporting import PASS_THROUGH_MODULES, emit, find_call_site, module_matches

ORIGINAL_ATTR = "__signal_guard_original__"
SEND_METHODS = ("send", "send_robust")
ASYNC_SEND_METHODS = ("asend", "asend_robust")

# A dispatch already on the stack this many times for the same object, signal
# and sender is a loop. Two allows the common "save once more in post_save".
MAX_REENTRY = 2

_DISPATCHER_MODULE = "django.dispatch.dispatcher"
# Frames that are not the project's own code when looking for the line that
# sent a signal again: Django itself (Model.save() and so on) and this package.
_NOT_USER_CODE = (*PASS_THROUGH_MODULES, "django")


class _Dispatch:
    # A plain class with __slots__: one is made per send(), and a frozen
    # dataclass costs several times as much to create.
    __slots__ = ("frame", "instance", "sender", "signal")

    def __init__(
        self,
        signal: Signal,
        sender: Any,
        instance: Any,
        frame: FrameType | None,
    ) -> None:
        self.signal = signal
        self.sender = sender
        self.instance = instance
        # The wrapper's frame. Frames between two dispatches belong to the
        # receiver that sent the inner one.
        self.frame = frame


@dataclass(frozen=True)
class RunningReceiver:
    path: str
    location: str | None
    module: str


@dataclass(frozen=True)
class Hop:
    """One signal sent along a recursion chain."""

    signal_name: str
    target: str
    # The receiver that sent the next signal, and how. None when it could not
    # be found, for example across a sync/async boundary.
    receiver: str | None
    receiver_location: str | None
    call: str | None
    sent_at: str | None


_stack: ContextVar[tuple[_Dispatch, ...]] = ContextVar(
    "signal_guard_dispatch_stack", default=()
)


def _same_object(a: Any, b: Any) -> bool:
    if a is b:
        return True
    if a is None or b is None or type(a) is not type(b):
        return False
    # The same row, even when the receiver fetched a fresh object for it. Read
    # now rather than at send time: pre_save runs before an insert sets the pk.
    pk = getattr(a, "pk", None)
    return pk is not None and pk == getattr(b, "pk", None)


def _matches(dispatch: _Dispatch, current: _Dispatch) -> bool:
    return (
        dispatch.signal is current.signal
        and _make_id(dispatch.sender) == _make_id(current.sender)
        and _same_object(dispatch.instance, current.instance)
    )


def _push(signal: Signal, sender: Any, named: dict[str, Any], frame: FrameType):
    entry = _Dispatch(signal, sender, named.get("instance"), frame)
    stack = _stack.get()
    # Only compared when it could matter, so an ordinary send stays cheap.
    if len(stack) >= MAX_REENTRY:
        matches = [i for i, d in enumerate(stack) if _matches(d, entry)]
        if len(matches) == MAX_REENTRY:
            _report_recursion(stack[matches[0] :], entry)
    return _stack.set((*stack, entry))


# --- describing frames ------------------------------------------------------


def _frames_from(frame: FrameType | None) -> list[FrameType]:
    frames = []
    while frame is not None:
        frames.append(frame)
        frame = frame.f_back
    return frames


def _module_of(frame: FrameType) -> str:
    return frame.f_globals.get("__name__", "")


def _qualname(frame: FrameType) -> str:
    code = frame.f_code
    qualname = getattr(code, "co_qualname", None)  # Python 3.11+
    if qualname is not None:
        return qualname
    # Python 3.10: ask the function that owns the code. Only runs when
    # something is reported, so the search through referrers is affordable.
    for referrer in gc.get_referrers(code):
        if getattr(referrer, "__code__", None) is code:
            return referrer.__qualname__
    return code.co_name


def _site(frame: FrameType) -> str:
    from .receivers import relative_path

    return f"{relative_path(frame.f_code.co_filename)}:{frame.f_lineno}"


def _call_label(frame: FrameType) -> str:
    """Name a call such as Order.save() from the frame it runs in."""
    code = frame.f_code
    self = frame.f_locals.get("self") if code.co_argcount else None
    if module_matches(_module_of(frame), ("signal_guard",)):
        name = "send"  # one of our wrappers around Signal.send()
    else:
        name = code.co_name
    if self is not None:
        return f"{type(self).__name__}.{name}()"
    return f"{_qualname(frame)}()"


def _receiver_between(
    frames: list[FrameType], outer: FrameType | None, inner: FrameType | None
) -> tuple[FrameType | None, list[FrameType]]:
    """Return the receiver frame between two dispatches, and the frames between.

    ``frames`` runs from the innermost frame outward. ``inner`` None means the
    current frame.
    """
    positions = {id(f): i for i, f in enumerate(frames)}
    if outer is None or id(outer) not in positions:
        return None, []
    end = positions[id(outer)]
    start = 0 if inner is None else positions.get(id(inner))
    if start is None:
        return None, []
    between = frames[start + 1 if inner is not None else 0 : end]
    # The receiver is the frame the dispatcher called, so the outermost frame
    # in range whose caller is the dispatcher.
    for index in range(len(between) - 1, -1, -1):
        frame = between[index]
        back = frame.f_back
        if back is None or _module_of(back) != _DISPATCHER_MODULE:
            continue
        if module_matches(_module_of(frame), ("signal_guard",)):
            # A wrapper of ours, such as @receiver_on_commit running at once
            # outside a transaction: the receiver is the project frame inside.
            for inner_frame in reversed(between[:index]):
                if not module_matches(_module_of(inner_frame), _NOT_USER_CODE):
                    return inner_frame, between
        return frame, between
    return None, between


def _describe_receiver(frame: FrameType) -> RunningReceiver:
    from .receivers import relative_path

    code = frame.f_code
    module = _module_of(frame)
    return RunningReceiver(
        path=f"{module}.{_qualname(frame)}",
        location=f"{relative_path(code.co_filename)}:{code.co_firstlineno}",
        module=module,
    )


def current_receiver() -> RunningReceiver | None:
    """The receiver running right now, if a signal is being dispatched."""
    stack = _stack.get()
    if not stack:
        return None
    frames = _frames_from(sys._getframe(1))
    frame, _ = _receiver_between(frames, stack[-1].frame, None)
    return None if frame is None else _describe_receiver(frame)


def is_dispatching() -> bool:
    return bool(_stack.get())


def innermost_dispatch_frame() -> FrameType | None:
    """The frame of the send() wrapper for the signal being dispatched now."""
    stack = _stack.get()
    return stack[-1].frame if stack else None


def _concrete(model: Any) -> Any:
    meta = getattr(model, "_meta", None)
    return getattr(meta, "concrete_model", None) or model


def save_dispatch_for(model: type) -> str | None:
    """ "pre_save" or "post_save" if one is being dispatched for ``model``."""
    concrete = _concrete(model)
    for dispatch in reversed(_stack.get()):
        if (dispatch.signal is post_save or dispatch.signal is pre_save) and _concrete(
            dispatch.sender
        ) is concrete:
            return "post_save" if dispatch.signal is post_save else "pre_save"
    return None


def delete_dispatch() -> tuple[str, str] | None:
    """(signal name, object) if pre_delete or post_delete is being dispatched."""
    for dispatch in reversed(_stack.get()):
        if dispatch.signal is pre_delete or dispatch.signal is post_delete:
            name = "pre_delete" if dispatch.signal is pre_delete else "post_delete"
            return name, _target_label(dispatch.sender, dispatch.instance)
    return None


# --- recursion --------------------------------------------------------------


def _target_label(sender: Any, instance: Any) -> str:
    from .registry import sender_label

    meta = getattr(instance, "_meta", None)
    if instance is not None and meta is not None:
        pk = getattr(instance, "pk", None)
        return f"{meta.label} #{pk}" if pk is not None else f"{meta.label} (unsaved)"
    label = sender_label(sender)
    return "any sender" if label == "*" else label


def _signal_names(signals: set[Signal]) -> dict[Signal, str]:
    from .inspection import all_signals

    named = all_signals()
    return {
        s: named.get(s, f"<{type(s).__name__}>").rpartition(".")[2] for s in signals
    }


def build_chain(
    stack: tuple[_Dispatch, ...], current: _Dispatch, frames: list[FrameType]
) -> tuple[Hop, ...]:
    chain = (*stack, current)
    names = _signal_names({d.signal for d in chain})
    hops = []
    for index, dispatch in enumerate(chain):
        receiver = location = call = sent_at = None
        if index + 1 < len(chain):
            inner = chain[index + 1].frame
            frame, between = _receiver_between(frames, dispatch.frame, inner)
            if frame is not None:
                described = _describe_receiver(frame)
                receiver, location = described.path, described.location
                # The innermost project frame is the line that sent again;
                # the frame it called is the call, such as Order.save().
                for i, candidate in enumerate(between):
                    if not module_matches(_module_of(candidate), _NOT_USER_CODE):
                        sent_at = _site(candidate)
                        callee = between[i - 1] if i > 0 else inner
                        if callee is not None:
                            call = _call_label(callee)
                        break
        hops.append(
            Hop(
                signal_name=names[dispatch.signal],
                target=_target_label(dispatch.sender, dispatch.instance),
                receiver=receiver,
                receiver_location=location,
                call=call,
                sent_at=sent_at,
            )
        )
    return tuple(hops)


def format_recursion(chain: tuple[Hop, ...]) -> str:
    first, last = chain[0], chain[-1]
    if first.receiver:
        head = f"{first.receiver} re-entered for {first.target}:"
    else:
        head = f"{first.signal_name} for {first.target} re-entered {MAX_REENTRY} times:"
    width = max(len(h.signal_name) for h in chain)
    lines = [head]
    for hop in chain[:-1]:
        receiver = f" → {hop.receiver}" if hop.receiver else ""
        where = f"  ({hop.receiver_location})" if hop.receiver_location else ""
        lines.append(f"  {hop.signal_name.ljust(width)}  {hop.target}{receiver}{where}")
        if hop.sent_at:
            lines.append(f"    {hop.call or 'sent'}  {hop.sent_at}")
    lines.append(f"  {last.signal_name.ljust(width)}  {last.target}  ← this send")
    lines.append(
        "Stop the loop with a guard (check update_fields or a flag on the "
        "instance), or write the change with QuerySet.update()."
    )
    return "\n".join(lines)


def _report_recursion(loop: tuple[_Dispatch, ...], current: _Dispatch) -> None:
    config = get_config()
    if config.recursion == "off":
        return
    frames = _frames_from(sys._getframe(0))
    chain = build_chain(loop, current, frames)
    message = format_recursion(chain)
    site = find_call_site(sys._getframe(0), _NOT_USER_CODE)
    emit(
        config.recursion,
        message,
        kind="recursion",
        location=site.location,
        error=lambda: SignalRecursionError(message, chain=chain),
        warning=SignalRecursionWarning,
        stacklevel=site.stacklevel,
        extra={"signal_guard_chain": [h.receiver for h in chain]},
    )


# --- installing -------------------------------------------------------------


def _wrap(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def send(self: Signal, sender: Any, **named: Any) -> Any:
        if not self.receivers:
            return original(self, sender, **named)
        token = _push(self, sender, named, sys._getframe(0))
        try:
            return original(self, sender, **named)
        finally:
            _stack.reset(token)

    setattr(send, ORIGINAL_ATTR, original)
    return send


def _wrap_async(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def asend(self: Signal, sender: Any, **named: Any) -> Any:
        if not self.receivers:
            return await original(self, sender, **named)
        token = _push(self, sender, named, sys._getframe(0))
        try:
            return await original(self, sender, **named)
        finally:
            _stack.reset(token)

    setattr(asend, ORIGINAL_ATTR, original)
    return asend


def install() -> None:
    """Wrap Signal.send() and friends. Safe to call more than once."""
    for names, wrap in ((SEND_METHODS, _wrap), (ASYNC_SEND_METHODS, _wrap_async)):
        for name in names:
            current = getattr(Signal, name)
            if not hasattr(current, ORIGINAL_ATTR):
                setattr(Signal, name, wrap(current))


def uninstall() -> None:
    for name in (*SEND_METHODS, *ASYNC_SEND_METHODS):
        original = getattr(getattr(Signal, name), ORIGINAL_ATTR, None)
        if original is not None:
            setattr(Signal, name, original)


def is_installed() -> bool:
    return all(
        hasattr(getattr(Signal, n), ORIGINAL_ATTR)
        for n in (*SEND_METHODS, *ASYNC_SEND_METHODS)
    )
