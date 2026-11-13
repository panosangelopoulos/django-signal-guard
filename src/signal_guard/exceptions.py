from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import Model

    from .receivers import SkippedReceiver


class SignalGuardError(Exception):
    """Base class for errors raised by django-signal-guard."""


class SignalBypassError(SignalGuardError):
    """A bulk write would skip pre_save or post_save receivers.

    Raised before the write reaches the database, so nothing is written.
    """

    def __init__(
        self,
        message: str,
        *,
        model: type[Model],
        method: str,
        receivers: tuple[SkippedReceiver, ...],
    ) -> None:
        super().__init__(message)
        self.model = model
        self.method = method
        self.receivers = receivers


class SignalBypassWarning(UserWarning):
    """Warning category used when ``SIGNAL_GUARD['BYPASS'] == 'warn'``."""
