"""pytest plugin: fixtures, a summary of reports, and a baseline for CI.

Installed with the package. The fixtures are always there; the rest does
nothing until asked for:

* ``--signal-guard-summary`` lists every report at the end of the run,
  grouped by the line it blames, with the tests that hit it.
* ``--signal-guard-baseline=FILE`` (or the ``signal_guard_baseline`` ini
  option) silences the reports listed in FILE and fails the run on any
  other. ``--signal-guard-update-baseline`` writes FILE from this run.

Both work with pytest-xdist: each worker sends what it saw to the controller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from .reporting import Report

WORKER_KEY = "signal_guard"
TESTS_SHOWN = 3
INI_BASELINE = "signal_guard_baseline"

# kind, where (path::function), fingerprint
Key = tuple[str, str, str]


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("signal-guard")
    group.addoption(
        "--signal-guard-summary",
        action="store_true",
        default=False,
        help="List django-signal-guard reports by call site at the end of the run.",
    )
    group.addoption(
        "--signal-guard-baseline",
        metavar="FILE",
        default=None,
        help="Silence the reports listed in FILE and fail the run on new ones.",
    )
    group.addoption(
        "--signal-guard-update-baseline",
        action="store_true",
        default=False,
        help="Silence every report and write them all to the baseline FILE.",
    )
    parser.addini(
        INI_BASELINE,
        help="Baseline file for django-signal-guard, relative to the ini file.",
        default=None,
    )


def pytest_configure(config: pytest.Config) -> None:
    summary = config.getoption("signal_guard_summary")
    update = config.getoption("signal_guard_update_baseline")
    baseline = _baseline(config, update)
    if summary or baseline is not None:
        config.pluginmanager.register(
            SignalGuardPlugin(config, summary, baseline), "signal_guard_session"
        )


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def signal_reports() -> Any:
    """Reports made during the test, collected instead of raised or warned.

    A ``signal_guard.testing.Reports`` list: filter it with ``.bypasses``,
    ``.side_effects`` and ``.recursions``, or call ``.assert_none()``.
    """
    from .testing import capture_reports

    with capture_reports() as reports:
        yield reports


@pytest.fixture
def sent_signals() -> Any:
    """Every signal sent during the test, as ``signal_guard.testing.SentSignals``."""
    from .testing import capture_signals

    with capture_signals() as sent:
        yield sent


# --- the summary ------------------------------------------------------------


@dataclass
class Row:
    kind: str
    location: str
    headline: str
    count: int = 0
    tests: list[str] = field(default_factory=list)


class Summary:
    """Reports counted by (kind, location)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Row] = {}

    def record(self, report: Report) -> None:
        self._add(
            kind=report.kind,
            location=report.location or "<unknown>",
            headline=report.message.partition("\n")[0],
            count=1,
            tests=[report.test] if report.test else [],
        )

    def _add(
        self,
        kind: str,
        location: str,
        headline: str,
        count: int,
        tests: list[str],
    ) -> None:
        row = self._rows.setdefault(
            (kind, location), Row(kind=kind, location=location, headline=headline)
        )
        row.count += count
        for test in tests:
            if test not in row.tests:
                row.tests.append(test)

    def dump(self) -> str:
        return json.dumps([vars(row) for row in self._rows.values()])

    def merge(self, dumped: str) -> None:
        for row in json.loads(dumped):
            self._add(**row)

    def rows(self) -> list[Row]:
        return sorted(self._rows.values(), key=lambda r: (-r.count, r.kind, r.location))

    def __bool__(self) -> bool:
        return bool(self._rows)


def _write_rows(write: Any, rows: list[Row]) -> None:
    for row in rows:
        tests = len(row.tests)
        reports = _plural(row.count, "report")
        write("")
        write(
            f"{row.location}  {row.kind}  {row.count} {reports}"
            f" in {tests} {_plural(tests, 'test')}"
        )
        write(f"    {row.headline}")
        for test in row.tests[:TESTS_SHOWN]:
            write(f"    - {test}")
        if tests > TESTS_SHOWN:
            write(f"    - and {tests - TESTS_SHOWN} more")


# --- the baseline -----------------------------------------------------------

HEADER = """\
# django-signal-guard baseline: the reports known when it was written.
# These are not raised, warned or logged; any other report fails the run.
# One line per report: kind | path::function | what. Regenerate with:
#   pytest --signal-guard-baseline={name} --signal-guard-update-baseline
"""


def key_of(report: Report) -> Key:
    where = report.where or report.location or "<unknown>"
    return (report.kind, where, report.fingerprint)


def read_baseline(path: Path) -> set[Key]:
    known = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(" | ", 2)
        if len(parts) == 3:
            known.add((parts[0], parts[1], parts[2]))
    return known


def write_baseline(path: Path, name: str, keys: set[Key]) -> None:
    lines = [" | ".join(key) for key in sorted(keys)]
    path.write_text(HEADER.format(name=name) + "".join(f"{x}\n" for x in lines))


class Baseline:
    def __init__(self, path: Path, name: str, update: bool) -> None:
        self.path = path
        self.name = name
        self.update = update
        self.known: set[Key] = set() if update else read_baseline(path)
        self.seen: set[Key] = set()
        self.new = Summary()

    def record(self, report: Report) -> None:
        key = key_of(report)
        self.seen.add(key)
        if not self.update and key not in self.known:
            self.new.record(report)

    def suppress(self, report: Report) -> bool:
        return self.update or key_of(report) in self.known

    def dump(self) -> dict[str, Any]:
        return {"seen": sorted(self.seen), "new": self.new.dump()}

    def merge(self, dumped: dict[str, Any]) -> None:
        self.seen.update(tuple(key) for key in dumped["seen"])  # type: ignore[misc]
        self.new.merge(dumped["new"])


def _baseline(config: pytest.Config, update: bool) -> Baseline | None:
    name = config.getoption("signal_guard_baseline")
    if name:
        path = Path(config.invocation_params.dir, name)
    else:
        name = config.getini(INI_BASELINE)
        if not name:
            if update:
                raise pytest.UsageError(
                    "--signal-guard-update-baseline needs a file: pass "
                    "--signal-guard-baseline=FILE or set signal_guard_baseline "
                    "in the pytest configuration."
                )
            return None
        root = config.inipath.parent if config.inipath else config.rootpath
        path = root / name
    if not update and not path.exists():
        raise pytest.UsageError(
            f"The signal-guard baseline {name} does not exist. Create it with "
            f"pytest --signal-guard-baseline={name} --signal-guard-update-baseline"
        )
    return Baseline(path, name, update)


# --- the session ------------------------------------------------------------


class SignalGuardPlugin:
    def __init__(
        self, config: pytest.Config, summary: bool, baseline: Baseline | None
    ) -> None:
        self.config = config
        self.summary = Summary() if summary else None
        self.baseline = baseline

    @property
    def is_worker(self) -> bool:
        return hasattr(self.config, "workeroutput")

    def record(self, report: Report) -> None:
        if self.summary is not None:
            self.summary.record(report)
        if self.baseline is not None:
            self.baseline.record(report)

    def suppress(self, report: Report) -> bool:
        return self.baseline is not None and self.baseline.suppress(report)

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        # Imported here: by now pytest-django has configured Django.
        from .reporting import LISTENERS, SUPPRESSORS

        LISTENERS.append(self.record)
        SUPPRESSORS.append(self.suppress)

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        from .reporting import LISTENERS, SUPPRESSORS

        if self.record in LISTENERS:
            LISTENERS.remove(self.record)
        if self.suppress in SUPPRESSORS:
            SUPPRESSORS.remove(self.suppress)

        if self.is_worker:  # a pytest-xdist worker: hand over to the controller
            self.config.workeroutput[WORKER_KEY] = json.dumps(  # type: ignore[attr-defined]
                {
                    "summary": self.summary.dump() if self.summary else None,
                    "baseline": self.baseline.dump() if self.baseline else None,
                }
            )
            return

        baseline = self.baseline
        if baseline is None:
            return
        if baseline.update:
            write_baseline(baseline.path, baseline.name, baseline.seen)
        elif baseline.new and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        dumped = getattr(node, "workeroutput", {}).get(WORKER_KEY)
        if not dumped:
            return
        data = json.loads(dumped)
        if self.summary is not None and data["summary"]:
            self.summary.merge(data["summary"])
        if self.baseline is not None and data["baseline"]:
            self.baseline.merge(data["baseline"])

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.is_worker:
            return
        write = terminalreporter.write_line
        if self.summary is not None:
            terminalreporter.write_sep("=", "signal-guard summary")
            rows = self.summary.rows()
            if rows:
                total = sum(row.count for row in rows)
                sites = _plural(len(rows), "call site")
                write(f"{total} {_plural(total, 'report')} at {len(rows)} {sites}.")
                _write_rows(write, rows)
            else:
                write("No reports.")
        if self.baseline is not None:
            self._write_baseline(terminalreporter)

    def _write_baseline(self, terminalreporter: Any) -> None:
        baseline = self.baseline
        assert baseline is not None
        write = terminalreporter.write_line
        if baseline.update:
            entries = _plural(len(baseline.seen), "entry", "entries")
            terminalreporter.write_sep("=", "signal-guard baseline")
            write(f"Wrote {len(baseline.seen)} {entries} to {baseline.name}.")
            return
        rows = baseline.new.rows()
        if rows:
            total = sum(row.count for row in rows)
            terminalreporter.write_sep(
                "=",
                f"signal-guard: {total} new {_plural(total, 'report')} "
                f"not in {baseline.name}",
                red=True,
            )
            _write_rows(write, rows)
            write("")
            write(
                "Fix them, or if they are intended, wrap the call in "
                "allow_bypass() / allow_side_effect(), or regenerate the "
                "baseline with --signal-guard-update-baseline."
            )
        else:
            terminalreporter.write_sep(
                "=",
                f"signal-guard: no new reports ({len(baseline.known)} known "
                f"in {baseline.name})",
            )
        stale = len(baseline.known - baseline.seen)
        if stale:
            entry = _plural(stale, "baseline entry was", "baseline entries were")
            write(
                f"{stale} {entry} not seen in this run: fixed, or not run. "
                "--signal-guard-update-baseline drops them."
            )


def _plural(count: int, one: str, many: str | None = None) -> str:
    if count == 1:
        return one
    return many if many is not None else f"{one}s"
