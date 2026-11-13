"""Find the code to blame for a problem, and report it in the configured mode."""

from __future__ import annotations

import gc
import logging
import os
import warnings
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import CodeType, FrameType
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
    # The qualified name of the function the frame runs.
    function: str | None = None

    @property
    def location(self) -> str | None:
        """``path:line``."""
        if self.filename is None:
            return None
        from .receivers import relative_path

        return f"{relative_path(self.filename)}:{self.lineno}"

    @property
    def where(self) -> str | None:
        """``path::function``: like ``location``, but stable when lines move."""
        if self.filename is None:
            return None
        from .receivers import relative_path

        return f"{relative_path(self.filename)}::{self.function or '<module>'}"


KINDS = ("bypass", "recursion", "side_effect")


@dataclass(frozen=True)
class Report:
    """One problem found by a check.

    Passed to captures (``signal_guard.testing``) and listeners (the pytest
    plugin) before it is raised, warned or logged.
    """

    kind: str  # one of KINDS
    location: str | None  # "path:line" of the code to blame
    message: str
    test: str | None  # the pytest node id running, if any
    # "path::function" of the code to blame: no line number.
    where: str | None = None
    # What went wrong, without line numbers, primary keys, addresses or
    # URLs, so the same problem reported twice has the same fingerprint.
    fingerprint: str = ""
    # The exception raise mode raises, with structured details: model,
    # method and receivers for a bypass, call and receiver for a side
    # effect, chain for a recursion.
    error: Exception | None = field(default=None, repr=False, compare=False)


# Called with every Report that no capture took, whatever the mode, before it
# is raised, warned or logged.
LISTENERS: list[Callable[[Report], None]] = []
# A report any of these returns True for is not raised, warned or logged.
SUPPRESSORS: list[Callable[[Report], bool]] = []


class Capture:
    """Collects the reports of some kinds that ``match``, instead of emitting them.

    Reports of those kinds that do not match go on as usual and are kept in
    ``passed``.
    """

    def __init__(
        self,
        kinds: tuple[str, ...] = KINDS,
        match: Callable[[Report], bool] | None = None,
        reports: list[Report] | None = None,
    ) -> None:
        self.kinds = kinds
        self.match = match
        self.reports: list[Report] = [] if reports is None else reports
        self.passed: list[Report] = []

    def offer(self, report: Report) -> bool:
        if report.kind not in self.kinds:
            return False
        if self.match is not None and not self.match(report):
            self.passed.append(report)
            return False
        self.reports.append(report)
        return True

    def caught(self, exc: BaseException | None) -> bool:
        """True if ``exc`` is the error of a report this capture took."""
        return exc is not None and any(r.error is exc for r in self.reports)


_captures: ContextVar[tuple[Capture, ...]] = ContextVar(
    "signal_guard_captures", default=()
)


def start_capture(capture: Capture) -> Token[tuple[Capture, ...]]:
    return _captures.set((*_captures.get(), capture))


def stop_capture(token: Token[tuple[Capture, ...]]) -> None:
    _captures.reset(token)


def code_qualname(code: CodeType) -> str:
    qualname = getattr(code, "co_qualname", None)  # Python 3.11+
    if qualname is not None:
        return qualname
    # Python 3.10: ask the function that owns the code. Only runs when
    # something is reported, so the search through referrers is affordable.
    for referrer in gc.get_referrers(code):
        if getattr(referrer, "__code__", None) is code:
            return referrer.__qualname__
    return code.co_name


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
                    function=code_qualname(frame.f_code),
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
                function=code_qualname(frame.f_code),
            )
        frame, depth = frame.f_back, depth + 1
    return None


def emit(
    mode: str,
    message: str,
    *,
    kind: str,
    site: CallSite,
    fingerprint: str,
    error: Callable[[], Exception],
    warning: type[Warning],
    extra: dict[str, Any] | None = None,
    stops: bool = False,
) -> None:
    """Raise, warn or log ``message`` according to ``mode``.

    ``site.stacklevel`` is relative to the caller of ``emit``. ``stops``
    marks a report whose code must not go on, such as a recursion: when a
    capture takes it, its error is still raised, and the capture swallows it.
    """
    exc = error()
    report = Report(
        kind=kind,
        location=site.location,
        message=message,
        test=current_test(),
        where=site.where,
        fingerprint=fingerprint,
        error=exc,
    )
    for capture in reversed(_captures.get()):
        if capture.offer(report):
            if stops:
                raise exc
            return
    for listener in LISTENERS:
        listener(report)
    if any(suppress(report) for suppress in SUPPRESSORS):
        return
    if mode == "raise":
        raise exc
    if mode == "warn":
        warnings.warn(message, warning, stacklevel=site.stacklevel + 1)
    elif mode == "log":
        fields = {
            "signal_guard_kind": kind,
            "signal_guard_location": report.location,
            "signal_guard_test": report.test,
            **(extra or {}),
        }
        logger.warning(message, stacklevel=site.stacklevel + 1, extra=fields)
