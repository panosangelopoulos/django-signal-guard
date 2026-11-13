"""Make Django signal failures loud."""

from .exceptions import (
    SideEffectBeforeCommitError,
    SideEffectBeforeCommitWarning,
    SignalBypassError,
    SignalBypassWarning,
    SignalGuardError,
    SignalRecursionError,
    SignalRecursionWarning,
)
from .guard import allow_bypass
from .on_commit import receiver_on_commit
from .receivers import SkippedReceiver, bulk_safe
from .side_effects import allow_side_effect

__all__ = [
    "SideEffectBeforeCommitError",
    "SideEffectBeforeCommitWarning",
    "SignalBypassError",
    "SignalBypassWarning",
    "SignalGuardError",
    "SignalRecursionError",
    "SignalRecursionWarning",
    "SkippedReceiver",
    "allow_bypass",
    "allow_side_effect",
    "bulk_safe",
    "receiver_on_commit",
]

__version__ = "0.3.0"
