"""Assertions for tests: about django-signal-guard's reports, and about signals.

Reports:

    from signal_guard.testing import assert_bypass, assert_no_reports

    def test_import_is_clean():
        with assert_no_reports():
            import_orders(rows)

    def test_reindex_job_is_flagged():
        with assert_bypass(Order, method="update"):
            Order.objects.update(indexed=False)

Signals:

    from signal_guard.testing import assert_sent

    def test_paying_sends_order_paid():
        with assert_sent(order_paid, sender=Order) as sent:
            order.pay()
        assert sent[0].kwargs["amount"] == 10

The same assertions are pytest fixtures (``signal_reports``,
``sent_signals``) and ``unittest`` methods (``SignalGuardAssertionsMixin``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import ContextDecorator, contextmanager
from fnmatch import fnmatchcase
from typing import Any

from django.db.models import Model
from django.dispatch import Signal

from . import tracing
from .conf import get_config
from .receivers import describe, live_receivers
from .registry import sender_label
from .reporting import KINDS, Capture, Report, start_capture, stop_capture
from .tracing import SentSignal, SignalCapture

__all__ = [
    "Report",
    "Reports",
    "SentSignal",
    "SentSignals",
    "SignalGuardAssertionsMixin",
    "assert_bypass",
    "assert_connected",
    "assert_no_reports",
    "assert_not_connected",
    "assert_not_sent",
    "assert_recursion",
    "assert_sent",
    "assert_side_effect",
    "capture_reports",
    "capture_signals",
]

_SETTING = {"bypass": "BYPASS", "recursion": "RECURSION", "side_effect": "SIDE_EFFECTS"}


# --- reports ----------------------------------------------------------------


class Reports(list[Report]):
    """A list of ``Report`` objects, with filters by kind."""

    def of(self, *kinds: str) -> Reports:
        return Reports(r for r in self if r.kind in kinds)

    @property
    def bypasses(self) -> Reports:
        return self.of("bypass")

    @property
    def side_effects(self) -> Reports:
        return self.of("side_effect")

    @property
    def recursions(self) -> Reports:
        return self.of("recursion")

    def assert_none(self, *kinds: str) -> None:
        """Fail if there are reports (of ``kinds``, if given)."""
        found = self.of(*kinds) if kinds else self
        if found:
            raise AssertionError(_describe_reports(found))


def _describe_reports(reports: Iterable[Report]) -> str:
    reports = list(reports)
    noun = "report" if len(reports) == 1 else "reports"
    parts = [f"{len(reports)} signal-guard {noun}:"]
    for report in reports:
        test = f"\n  in test {report.test}" if report.test else ""
        parts.append(f"{report.message}{test}")
    return "\n\n".join(parts)


def _check_kinds(kinds: tuple[str, ...]) -> tuple[str, ...]:
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValueError(
            f"Unknown report kind(s): {', '.join(map(repr, unknown))}. "
            f"Use {', '.join(map(repr, KINDS))}."
        )
    return kinds or KINDS


def _off_hint(kind: str) -> str:
    config = get_config()
    if not config.enabled:
        return " SIGNAL_GUARD['ENABLED'] is False, so nothing is checked."
    setting = _SETTING[kind]
    if getattr(config, setting.lower()) == "off":
        return f" SIGNAL_GUARD[{setting!r}] is 'off', so these are never reported."
    return ""


class _ReportCapture(ContextDecorator):
    """Captures reports while active; ``check`` runs at the end of the block.

    Captured reports are not raised, warned or logged. A report whose code
    must stop, such as a recursion, still raises its error inside the block;
    the block then ends there and the error is swallowed.
    """

    kinds: tuple[str, ...] = KINDS

    def match(self, report: Report) -> bool:
        return True

    def check(self, capture: Capture) -> None:
        pass

    def _recreate_cm(self) -> _ReportCapture:
        # A fresh copy per call when used as a decorator, so recursive or
        # concurrent calls do not share state.
        clone = type(self).__new__(type(self))
        clone.__dict__.update(self.__dict__)
        return clone

    def __enter__(self) -> Reports:
        reports = Reports()
        match = None if type(self).match is _ReportCapture.match else self.match
        self._capture = Capture(self.kinds, match, reports)
        self._token = start_capture(self._capture)
        return reports

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        stop_capture(self._token)
        swallowed = self._capture.caught(exc)
        if exc is None or swallowed:
            self.check(self._capture)
        return swallowed


class capture_reports(_ReportCapture):
    """Collect reports instead of raising, warning or logging them.

        with capture_reports() as reports:
            run_import()
        assert reports.bypasses == []

    With ``kinds`` ("bypass", "recursion", "side_effect"), only those are
    collected; others are emitted as usual. Settings still apply: a check
    set to "off" reports nothing.
    """

    def __init__(self, *kinds: str) -> None:
        self.kinds = _check_kinds(kinds)


class assert_no_reports(_ReportCapture):
    """Fail if the block causes any report (of ``kinds``, if given).

    A context manager or a decorator. Reports of other kinds are emitted as
    usual.
    """

    def __init__(self, *kinds: str) -> None:
        self.kinds = _check_kinds(kinds)

    def check(self, capture: Capture) -> None:
        if capture.reports:
            raise AssertionError(_describe_reports(capture.reports))


class _AssertReported(_ReportCapture):
    """Fail unless the block causes a matching report of ``kind``."""

    kind: str
    count: int | None

    @property
    def kinds(self) -> tuple[str, ...]:  # type: ignore[override]
        return (self.kind,)

    def describe(self) -> str:
        raise NotImplementedError

    def check(self, capture: Capture) -> None:
        got = len(capture.reports)
        if self.count is None and got:
            return
        if self.count is not None and got == self.count:
            return
        noun = self.kind.replace("_", " ")
        if self.count is None:
            message = f"Expected a {noun} {self.describe()}, but none was reported."
        else:
            message = f"Expected {self.count} {noun} {self.describe()}, got {got}."
        if capture.passed:
            message += "\n\nReported instead:\n\n" + "\n\n".join(
                r.message for r in capture.passed
            )
        raise AssertionError(message + _off_hint(self.kind))


def _label(model: type[Model] | str) -> str:
    return model if isinstance(model, str) else model._meta.label


class assert_bypass(_AssertReported):
    """Fail unless the block makes a bulk write that skips receivers.

        with assert_bypass(Order, method="update", receivers=["*.reindex_order"]):
            Order.objects.update(status="paid")

    ``model`` is a model class or an ``"app_label.Model"`` label. Each of
    ``receivers`` is an fnmatch pattern that must match one of the skipped
    receivers' dotted paths. ``count`` asks for exactly that many reports
    instead of at least one. Bypasses that don't match are emitted as usual.
    """

    kind = "bypass"

    def __init__(
        self,
        model: type[Model] | str | None = None,
        *,
        method: str | None = None,
        receivers: Iterable[str] = (),
        count: int | None = None,
    ) -> None:
        self.model = None if model is None else _label(model)
        self.method = method
        self.receivers = tuple(receivers)
        self.count = count

    def match(self, report: Report) -> bool:
        error: Any = report.error
        if self.model is not None and error.model._meta.label != self.model:
            return False
        if self.method is not None and error.method != self.method:
            return False
        paths = [r.path for r in error.receivers]
        return all(
            any(fnmatchcase(p, pattern) for p in paths) for pattern in self.receivers
        )

    def describe(self) -> str:
        parts = [f"of {self.model}" if self.model else "of any model"]
        if self.method:
            parts.append(f"by {self.method}()")
        if self.receivers:
            parts.append(f"skipping {', '.join(self.receivers)}")
        return " ".join(parts)


class assert_side_effect(_AssertReported):
    """Fail unless the block causes a side effect before commit.

    ``call`` is an fnmatch pattern for the call as reports name it, such as
    ``"Email*"``, ``"Celery task shop.tasks.*"`` or ``"HTTP POST *"``.
    ``receiver`` is a pattern for the receiver's dotted path.
    """

    kind = "side_effect"

    def __init__(
        self,
        call: str | None = None,
        *,
        receiver: str | None = None,
        count: int | None = None,
    ) -> None:
        self.call = call
        self.receiver = receiver
        self.count = count

    def match(self, report: Report) -> bool:
        error: Any = report.error
        if self.call is not None and not fnmatchcase(error.call, self.call):
            return False
        if self.receiver is None:
            return True
        return error.receiver is not None and fnmatchcase(error.receiver, self.receiver)

    def describe(self) -> str:
        parts = [f"matching {self.call!r}" if self.call else ""]
        if self.receiver:
            parts.append(f"in a receiver matching {self.receiver!r}")
        return " ".join(p for p in parts if p) or "of any kind"


class assert_recursion(_AssertReported):
    """Fail unless a receiver re-enters itself in the block.

    The loop is stopped, as raise mode would, and the block ends there.
    ``receiver`` is a pattern that one receiver in the chain must match.
    """

    kind = "recursion"

    def __init__(self, receiver: str | None = None, *, count: int | None = None):
        self.receiver = receiver
        self.count = count

    def match(self, report: Report) -> bool:
        if self.receiver is None:
            return True
        error: Any = report.error
        return any(
            hop.receiver is not None and fnmatchcase(hop.receiver, self.receiver)
            for hop in error.chain
        )

    def describe(self) -> str:
        return f"through {self.receiver!r}" if self.receiver else "of any receiver"


# --- signals sent -----------------------------------------------------------


class SentSignals(list[SentSignal]):
    """A list of ``SentSignal`` objects, oldest first."""

    def of(self, signal: Signal) -> SentSignals:
        return SentSignals(s for s in self if s.signal is signal)


@contextmanager
def capture_signals(*signals: Signal, sender: Any = None) -> Iterator[SentSignals]:
    """Record the signals sent in the block, with their arguments.

        with capture_signals(post_save, sender=Order) as sent:
            order.save()
        assert sent[0].kwargs["created"] is False

    With no signals, records every signal; ``sender`` limits it to one
    sender. Signals with no receivers are recorded too. Each ``SentSignal``
    has ``signal``, ``signal_name``, ``sender``, ``kwargs``, ``instance`` and
    ``responses``.
    """
    installed_here = not tracing.is_installed()
    if installed_here:
        tracing.install()
    capture = SignalCapture(signals, sender)
    sent = SentSignals()
    capture.sent = sent  # type: ignore[assignment]
    token = tracing.start_signal_capture(capture)
    try:
        yield sent
    finally:
        tracing.stop_signal_capture(token)
        if installed_here:
            tracing.uninstall()


def _signal_label(signal: Signal, sender: Any, kwargs: dict[str, Any]) -> str:
    name = tracing.signal_name(signal)
    text = name if sender is None else f"{name} from {sender_label(sender)}"
    if kwargs:
        text += " with " + ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return text


def _matches(entry: SentSignal, kwargs: dict[str, Any]) -> bool:
    return all(
        key in entry.kwargs and entry.kwargs[key] == value
        for key, value in kwargs.items()
    )


@contextmanager
def assert_sent(
    signal: Signal, *, sender: Any = None, count: int | None = None, **kwargs: Any
) -> Iterator[SentSignals]:
    """Fail unless the block sends ``signal`` (from ``sender``, with ``kwargs``).

        with assert_sent(post_save, sender=Order, created=True) as sent:
            Order.objects.create()

    Each keyword must equal the argument the signal was sent with.
    ``count`` asks for exactly that many sends instead of at least one. The
    matching sends are yielded.
    """
    matched = SentSignals()
    with capture_signals(signal, sender=sender) as sent:
        yield matched
    matched.extend(s for s in sent if _matches(s, kwargs))
    if (count is None and matched) or (count is not None and len(matched) == count):
        return
    label = _signal_label(signal, sender, kwargs)
    if count is None:
        message = f"Expected {label} to be sent, but it was not."
    else:
        message = f"Expected {label} to be sent {count} times, got {len(matched)}."
    others = [s for s in sent if s not in matched]
    if others:
        message += "\nSent instead:\n" + "\n".join(
            "  " + _signal_label(s.signal, s.sender, s.kwargs) for s in others
        )
    raise AssertionError(message)


@contextmanager
def assert_not_sent(
    signal: Signal, *, sender: Any = None, **kwargs: Any
) -> Iterator[None]:
    """Fail if the block sends ``signal`` (from ``sender``, with ``kwargs``)."""
    with capture_signals(signal, sender=sender) as sent:
        yield
    matched = [s for s in sent if _matches(s, kwargs)]
    if matched:
        times = "time" if len(matched) == 1 else "times"
        raise AssertionError(
            f"{_signal_label(signal, None, {})} was sent {len(matched)} {times}: "
            + "; ".join(_signal_label(s.signal, s.sender, s.kwargs) for s in matched)
        )


# --- receivers connected ----------------------------------------------------


def _connected(
    signal: Signal, receiver: Callable[..., Any] | str, sender: Any
) -> list[str]:
    paths = []
    for live in live_receivers(signal, sender):
        path = describe("", live).path
        if isinstance(receiver, str):
            if fnmatchcase(path, receiver):
                paths.append(path)
        elif live == receiver or getattr(live, "__wrapped__", None) == receiver:
            paths.append(path)
    return paths


def _receiver_label(receiver: Callable[..., Any] | str) -> str:
    return receiver if isinstance(receiver, str) else describe("", receiver).path


def assert_connected(
    signal: Signal, receiver: Callable[..., Any] | str, *, sender: Any = None
) -> None:
    """Fail unless ``receiver`` would run when ``signal`` is sent by ``sender``.

    ``receiver`` is the function itself or an fnmatch pattern for its dotted
    path. Receivers connected for every sender count. Catches the receiver
    whose module is never imported.
    """
    if not _connected(signal, receiver, sender):
        name = _signal_label(signal, sender, {})
        raise AssertionError(f"{_receiver_label(receiver)} is not connected to {name}.")


def assert_not_connected(
    signal: Signal, receiver: Callable[..., Any] | str, *, sender: Any = None
) -> None:
    """Fail if ``receiver`` would run when ``signal`` is sent by ``sender``."""
    found = _connected(signal, receiver, sender)
    if found:
        name = _signal_label(signal, sender, {})
        raise AssertionError(f"{', '.join(found)} is connected to {name}.")


# --- unittest ---------------------------------------------------------------


class SignalGuardAssertionsMixin:
    """The assertions as ``unittest`` methods, for ``TestCase`` classes.

    Mix it into a test case:

        class OrderTests(SignalGuardAssertionsMixin, TestCase):
            def test_import(self):
                with self.assertNoSignalReports():
                    import_orders(rows)

    Each method returns the matching assertion from this module.
    """

    def assertNoSignalReports(self, *kinds: str) -> assert_no_reports:
        return assert_no_reports(*kinds)

    def assertBypass(self, *args: Any, **kwargs: Any) -> assert_bypass:
        return assert_bypass(*args, **kwargs)

    def assertSideEffect(self, *args: Any, **kwargs: Any) -> assert_side_effect:
        return assert_side_effect(*args, **kwargs)

    def assertRecursion(self, *args: Any, **kwargs: Any) -> assert_recursion:
        return assert_recursion(*args, **kwargs)

    def assertSignalSent(self, *args: Any, **kwargs: Any) -> Any:
        return assert_sent(*args, **kwargs)

    def assertSignalNotSent(self, *args: Any, **kwargs: Any) -> Any:
        return assert_not_sent(*args, **kwargs)

    def assertReceiverConnected(self, *args: Any, **kwargs: Any) -> None:
        assert_connected(*args, **kwargs)

    def assertReceiverNotConnected(self, *args: Any, **kwargs: Any) -> None:
        assert_not_connected(*args, **kwargs)
