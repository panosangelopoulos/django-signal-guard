"""Report side effects that a receiver causes before the transaction commits.

A receiver that sends an email, queues a Celery task or calls a webhook while
the transaction is still open has two bugs waiting to happen:

* if the transaction rolls back, the email has already gone out;
* a Celery worker or webhook consumer can read the database before the row is
  committed, and fail with ``DoesNotExist``.

The check wraps the calls that cause those side effects. When one runs inside
a signal receiver, and the receiver runs inside a real transaction (not the
one a ``TestCase`` wraps each test in), it is reported.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import sys
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.db import connections

from . import tracing
from .allowance import Allowance
from .conf import get_config
from .exceptions import SideEffectBeforeCommitError, SideEffectBeforeCommitWarning
from .inspection import is_first_party
from .reporting import (
    PASS_THROUGH_MODULES,
    emit,
    find_call_site,
    find_first_party_site,
)

ORIGINAL_ATTR = "__signal_guard_original__"

# Library code between the project and the side effect, skipped when looking
# for the line to blame.
_LIBRARY_MODULES = (*PASS_THROUGH_MODULES, "django", "celery", "requests", "httpx")

_inside_side_effect: ContextVar[bool] = ContextVar(
    "signal_guard_inside_side_effect", default=False
)


class allow_side_effect(Allowance):
    """Allow side effects before the commit, and record why.

    For the rare side effect that must happen inside the transaction, such as
    a vendor call whose refusal has to roll the delete back:

        @receiver(pre_delete, sender=Subscription)
        @allow_side_effect(reason="a refused cancel must roll the delete back")
        def cancel_at_vendor(sender, instance, **kwargs):
            vendor.cancel(instance.vendor_id)

    Works as a context manager, a decorator on sync functions and as
    ``async with`` in async code.
    """

    active = ContextVar("signal_guard_side_effect_allowances", default=())

    def __init__(self, *, reason: str) -> None:
        super().__init__(
            reason=reason, example="a refused cancel must roll the delete back"
        )

    def __repr__(self) -> str:
        return f"<allow_side_effect: {self.reason!r}>"


@dataclass(frozen=True)
class Kind:
    verb: str
    consequence: str


EMAIL = Kind("is sent", "The email still goes out if the transaction rolls back.")
TASK = Kind(
    "is queued",
    "A worker can run it before the row is committed, and it still runs if the "
    "transaction rolls back.",
)
HTTP = Kind(
    "is made",
    "The other side can read your database before the row is committed, and "
    "the request is not undone if the transaction rolls back.",
)
CUSTOM = Kind("runs", "It still happens if the transaction rolls back.")


def _email(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    message = args[0] if args else None
    to = ", ".join(getattr(message, "to", None) or ())
    return f"Email to {to}" if to else "Email"


def _mass_email(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    return "Email (send_mass_mail)"


def _celery(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    task = args[0] if args else None
    return f"Celery task {getattr(task, 'name', None) or type(task).__name__}"


def _requests(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    method = args[1] if len(args) > 1 else kwargs.get("method", "?")
    url = args[2] if len(args) > 2 else kwargs.get("url", "?")
    return f"HTTP {str(method).upper()} {url}"


def _httpx(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    request = args[1] if len(args) > 1 else kwargs.get("request")
    return f"HTTP {getattr(request, 'method', '?')} {getattr(request, 'url', '?')}"


def _http_key(method: object, url: object) -> str:
    host = urlsplit(str(url)).netloc or str(url)
    return f"HTTP {str(method).upper()} {host}"


def _requests_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    method = args[1] if len(args) > 1 else kwargs.get("method", "?")
    url = args[2] if len(args) > 2 else kwargs.get("url", "?")
    return _http_key(method, url)


def _httpx_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    request = args[1] if len(args) > 1 else kwargs.get("request")
    return _http_key(getattr(request, "method", "?"), getattr(request, "url", "?"))


def _same(text: str) -> Callable[[tuple[Any, ...], dict[str, Any]], str]:
    return lambda args, kwargs: text


@dataclass(frozen=True)
class Target:
    path: str  # dotted path of the function or method to wrap
    kind: Kind
    describe: Callable[[tuple[Any, ...], dict[str, Any]], str]
    # Only wrapped when its top-level package is installed.
    optional: bool = False
    # Like describe, without addresses, URLs or ids. Defaults to describe.
    key: Callable[[tuple[Any, ...], dict[str, Any]], str] | None = None


BUILTIN_TARGETS = (
    Target(
        "django.core.mail.message.EmailMessage.send", EMAIL, _email, key=_same("Email")
    ),
    Target("django.core.mail.send_mass_mail", EMAIL, _mass_email),
    Target("celery.app.task.Task.apply_async", TASK, _celery, optional=True),
    Target(
        "requests.sessions.Session.request",
        HTTP,
        _requests,
        optional=True,
        key=_requests_key,
    ),
    Target("httpx.Client.send", HTTP, _httpx, optional=True, key=_httpx_key),
    Target("httpx.AsyncClient.send", HTTP, _httpx, optional=True, key=_httpx_key),
)


def _custom_target(path: str) -> Target:
    def describe(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        return f"{path}()"

    return Target(path, CUSTOM, describe)


# --- finding the open transaction -------------------------------------------


def uncommitted_alias() -> str | None:
    """The alias of a database with a real transaction open, if any.

    ``TestCase`` wraps every test in transactions of its own. Those are marked
    ``_from_testcase`` and do not count: in production that code would run in
    autocommit mode.
    """
    for connection in connections.all(initialized_only=True):
        if not connection.in_atomic_block:
            continue
        blocks = getattr(connection, "atomic_blocks", ())
        if any(not getattr(block, "_from_testcase", False) for block in blocks):
            return connection.alias
    return None


# --- the check --------------------------------------------------------------


def format_message(
    what: str,
    kind: Kind,
    alias: str,
    receiver: tracing.RunningReceiver | None,
    called_at: str | None,
    deleting: tuple[str, str] | None = None,
) -> str:
    lines = [f"{what} {kind.verb} before the transaction on {alias!r} commits."]
    if receiver is not None:
        where = f"  ({receiver.location})" if receiver.location else ""
        lines.append(f"  receiver   {receiver.path}{where}")
    if called_at:
        lines.append(f"  called at  {called_at}")
    lines.append(kind.consequence)
    if kind is TASK and deleting is not None:
        signal_name, target = deleting
        lines.append(
            f"It runs during {signal_name} for {target}. "
            "transaction.on_commit() alone will not fix it: after the commit "
            "the row is gone, so the task must not re-read it. Pass the task "
            "the values it needs, then queue it with on_commit()."
        )
    else:
        lines.append(
            "Connect the receiver with @signal_guard.receiver_on_commit, or wrap "
            "the call in transaction.on_commit()."
        )
    return "\n".join(lines)


def check_side_effect(
    target: Target, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    if not tracing.is_dispatching():
        return
    config = get_config()
    if not config.enabled or config.side_effects == "off":
        return
    alias = uncommitted_alias()
    if alias is None:
        return

    if allow_side_effect.entered():
        return

    receiver = tracing.current_receiver()
    here = sys._getframe(0)
    site = find_call_site(here, _LIBRARY_MODULES)
    if receiver is not None:
        if any(fnmatchcase(receiver.path, p) for p in config.ignore_receivers):
            return
        internal = not is_first_party(receiver.module)
    else:
        internal = site.internal
    if internal and not config.check_library_calls:
        return

    # Blame the project's own line, not a vendor SDK it called, when there is
    # one inside the receiver.
    blamed = find_first_party_site(here, tracing.innermost_dispatch_frame()) or site
    what = target.describe(args, kwargs)
    called_at = blamed.location
    message = format_message(
        what, target.kind, alias, receiver, called_at, tracing.delete_dispatch()
    )
    key = (target.key or target.describe)(args, kwargs)
    emit(
        config.side_effects,
        message,
        kind="side_effect",
        site=blamed,
        fingerprint=f"{receiver.path}: {key}" if receiver else key,
        error=lambda: SideEffectBeforeCommitError(
            message, call=what, receiver=receiver.path if receiver else None
        ),
        warning=SideEffectBeforeCommitWarning,
        extra={
            "signal_guard_call": what,
            "signal_guard_receiver": receiver.path if receiver else None,
        },
    )


# --- installing -------------------------------------------------------------


def _wrap(target: Target, original: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if _inside_side_effect.get():
                return await original(*args, **kwargs)
            check_side_effect(target, args, kwargs)
            token = _inside_side_effect.set(True)
            try:
                return await original(*args, **kwargs)
            finally:
                _inside_side_effect.reset(token)

        wrapper: Callable[..., Any] = async_wrapper
    else:

        @functools.wraps(original)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            # A side effect made by another one, such as an HTTP call inside a
            # SIDE_EFFECT_CALLS function, is reported once, for the outer one.
            if _inside_side_effect.get():
                return original(*args, **kwargs)
            check_side_effect(target, args, kwargs)
            token = _inside_side_effect.set(True)
            try:
                return original(*args, **kwargs)
            finally:
                _inside_side_effect.reset(token)

        wrapper = sync_wrapper

    setattr(wrapper, ORIGINAL_ATTR, original)
    return wrapper


def _resolve(path: str) -> tuple[Any, str]:
    """Import ``path`` and return (owner, attribute name)."""
    parts = path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split])
        try:
            owner: Any = importlib.import_module(module_name)
        except ImportError:
            continue
        try:
            for attr in parts[split:-1]:
                owner = getattr(owner, attr)
            getattr(owner, parts[-1])
        except AttributeError:
            break
        return owner, parts[-1]
    raise ImportError(path)


# (owner, attribute, value before wrapping) for each wrapped target.
_PATCHED: list[tuple[Any, str, Any]] = []


def _patch(target: Target) -> None:
    owner, attr = _resolve(target.path)
    raw = inspect.getattr_static(owner, attr)
    if hasattr(getattr(raw, "__func__", raw), ORIGINAL_ATTR):
        return  # already wrapped
    if isinstance(raw, staticmethod | classmethod):
        wrapped: Any = type(raw)(_wrap(target, raw.__func__))
    else:
        wrapped = _wrap(target, raw)
    setattr(owner, attr, wrapped)
    _PATCHED.append((owner, attr, raw))


def install(extra_paths: tuple[str, ...] = ()) -> None:
    """Wrap the built-in side effects and ``SIDE_EFFECT_CALLS``."""
    for target in BUILTIN_TARGETS:
        package = target.path.partition(".")[0]
        if target.optional and importlib.util.find_spec(package) is None:
            continue
        try:
            _patch(target)
        except ImportError:  # an optional library without that attribute
            if not target.optional:
                raise
    for path in extra_paths:
        try:
            _patch(_custom_target(path))
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"SIGNAL_GUARD['SIDE_EFFECT_CALLS'] lists {path!r}, which cannot "
                "be imported. Use the dotted path of a function or a method, "
                "such as 'shop.webhooks.post' or 'shop.payments.Client.charge'."
            ) from exc


def uninstall() -> None:
    while _PATCHED:
        owner, attr, raw = _PATCHED.pop()
        setattr(owner, attr, raw)


def is_installed() -> bool:
    return bool(_PATCHED)
