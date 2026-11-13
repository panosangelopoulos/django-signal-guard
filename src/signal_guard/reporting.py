"""Find the code to blame for a problem, and report it in the configured mode."""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any

from .inspection import is_first_party

logger = logging.getLogger("signal_guard")

# Frames that only forward a call: this package, the manager proxy that turns
# Order.objects.update() into a QuerySet call, and the machinery that runs
# sync code for abulk_create() and friends.
PASS_THROUGH_MODULES = (
    "signal_guard",
    "django.db.models.manager",
    "asgiref",
    "concurrent",
    "threading",
)


@dataclass(frozen=True)
class CallSite:
    filename: str | None
    lineno: int | None
    # True when the call comes from Django or an installed package.
    internal: bool
    stacklevel: int
    # The outermost frame skipped on the way, such as an update() override,
    # as "path:line (Class.method())".
    through: str | None = None

    @property
    def location(self) -> str | None:
        if self.filename is None:
            return None
        from .receivers import relative_path

        return f"{relative_path(self.filename)}:{self.lineno}"


@dataclass(frozen=True)
class Report:
    """One problem reported, as passed to listeners such as the pytest plugin."""

    kind: str  # "bypass", "recursion" or "side_effect"
    location: str | None  # "path:line" of the code to blame
    message: str
    test: str | None  # the pytest node id running, if any


# Called with every Report, whatever the mode, before it is raised, warned
# or logged.
LISTENERS: list[Callable[[Report], None]] = []


def current_test() -> str | None:
    """The node id of the pytest test running now, from PYTEST_CURRENT_TEST."""
    value = os.environ.get("PYTEST_CURRENT_TEST")
    if not value:
        return None
    # "path::test_name[params] (call)": drop the phase.
    return value.rsplit(" (", 1)[0] if value.endswith(")") else value


def module_matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def find_call_site(
    start: FrameType | None,
    pass_through: tuple[str, ...] = PASS_THROUGH_MODULES,
    skip: Callable[[FrameType], str | None] | None = None,
) -> CallSite:
    """Walk out from ``start`` to the first frame that is not pass-through.

    ``skip`` is asked about each candidate frame; a frame it describes (it
    returns a label, not None) is walked past and remembered as ``through``.

    ``stacklevel`` counts ``start`` as 1, matching ``warnings.warn`` and
    ``logging`` when they are called from the function that owns ``start``.
    """
    frame, depth = start, 1
    through = None
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not module_matches(module, pass_through):
            label = skip(frame) if skip is not None else None
            if label is None:
                return CallSite(
                    filename=frame.f_code.co_filename,
                    lineno=frame.f_lineno,
                    internal=not is_first_party(module),
                    stacklevel=depth,
                    through=through,
                )
            from .receivers import relative_path

            where = f"{relative_path(frame.f_code.co_filename)}:{frame.f_lineno}"
            through = f"{where} ({label})"
        frame, depth = frame.f_back, depth + 1
    # Only forwarding frames, for example a sync_to_async worker thread.
    return CallSite(filename=None, lineno=None, internal=False, stacklevel=1)


def find_first_party_site(
    start: FrameType | None, stop: FrameType | None
) -> CallSite | None:
    """The first frame of the project's own code from ``start`` out to ``stop``.

    None if every frame up to ``stop`` (not included) belongs to a library.
    """
    frame, depth = start, 1
    while frame is not None and frame is not stop:
        module = frame.f_globals.get("__name__", "")
        if not module_matches(module, PASS_THROUGH_MODULES) and is_first_party(module):
            return CallSite(
                filename=frame.f_code.co_filename,
                lineno=frame.f_lineno,
                internal=False,
                stacklevel=depth,
            )
        frame, depth = frame.f_back, depth + 1
    return None


def emit(
    mode: str,
    message: str,
    *,
    kind: str,
    location: str | None,
    error: Callable[[], Exception],
    warning: type[Warning],
    stacklevel: int,
    extra: dict[str, Any] | None = None,
) -> None:
    """Raise, warn or log ``message`` according to ``mode``.

    ``stacklevel`` is relative to the caller of ``emit``.
    """
    test = current_test()
    if LISTENERS:
        report = Report(kind=kind, location=location, message=message, test=test)
        for listener in LISTENERS:
            listener(report)
    if mode == "raise":
        raise error()
    if mode == "warn":
        warnings.warn(message, warning, stacklevel=stacklevel + 1)
    elif mode == "log":
        fields = {
            "signal_guard_kind": kind,
            "signal_guard_location": location,
            "signal_guard_test": test,
            **(extra or {}),
        }
        logger.warning(message, stacklevel=stacklevel + 1, extra=fields)
