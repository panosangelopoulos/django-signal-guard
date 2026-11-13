"""``@receiver_on_commit``: run a receiver once the transaction commits."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from django.db import DEFAULT_DB_ALIAS, transaction
from django.dispatch import Signal

F = TypeVar("F", bound=Callable[..., Any])

ON_COMMIT_ATTR = "_signal_guard_on_commit"
DEFERRED_ATTR = "_signal_guard_deferred"


def receiver_on_commit(
    signal: Signal | list[Signal] | tuple[Signal, ...],
    *,
    using: str | None = None,
    robust: bool = False,
    **connect_kwargs: Any,
) -> Callable[[F], F]:
    """Connect a receiver that runs after the current transaction commits.

    ::

        @receiver_on_commit(post_save, sender=Order)
        def send_confirmation(sender, instance, created, **kwargs):
            if created:
                send_order_email.delay(instance.pk)

    Works like ``@receiver``: it takes the same ``sender``, ``weak`` and
    ``dispatch_uid`` arguments, and the receiver gets the same arguments the
    signal sent. The call is handed to ``transaction.on_commit()`` on the
    database the signal names in ``using`` (model signals do), else the
    ``using`` given here, else the default database. Outside a transaction it
    runs at once, as ``on_commit()`` does. If the transaction rolls back, it
    never runs.

    ``robust=True`` is passed on to ``on_commit()``: an exception in this
    receiver is logged instead of stopping the other commit callbacks.

    The decorated function is returned unchanged, so it can still be called
    directly in tests.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def deferred(sender: Any, **named: Any) -> None:
            alias = named.get("using") or using or DEFAULT_DB_ALIAS

            # A named function rather than functools.partial: robust
            # on_commit() logs the callback's __qualname__, which a partial
            # lacks (Django 5.2 then fails while logging).
            @functools.wraps(func)
            def run() -> None:
                func(sender=sender, **named)

            transaction.on_commit(run, using=alias, robust=robust)

        setattr(deferred, ON_COMMIT_ATTR, True)
        # Django holds receivers by weak reference. The function keeps its
        # wrapper alive, so the wrapper lives exactly as long as the function.
        setattr(func, DEFERRED_ATTR, deferred)

        signals = signal if isinstance(signal, list | tuple) else (signal,)
        for each in signals:
            each.connect(deferred, **connect_kwargs)
        return func

    return decorator


def is_on_commit(receiver: Any) -> bool:
    return getattr(receiver, ON_COMMIT_ATTR, False)
