"""pytest plugin: ``--signal-guard-summary`` lists reports by call site.

Installed with the package and inactive until the flag is passed. Each
report is counted under the line it blames, with the tests that hit it, and
the summary is printed at the end of the session, most reports first. Works
with pytest-xdist: each worker sends its counts to the controller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from .reporting import Report

WORKER_KEY = "signal_guard_summary"
TESTS_SHOWN = 3


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("signal-guard")
    group.addoption(
        "--signal-guard-summary",
        action="store_true",
        default=False,
        help="List django-signal-guard reports by call site at the end of the run.",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("signal_guard_summary"):
        config.pluginmanager.register(SummaryPlugin(config), "signal_guard_summary")


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


class SummaryPlugin:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.summary = Summary()

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        # Imported here: by now pytest-django has configured Django.
        from .reporting import LISTENERS

        LISTENERS.append(self.summary.record)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        from .reporting import LISTENERS

        if self.summary.record in LISTENERS:
            LISTENERS.remove(self.summary.record)
        workeroutput = getattr(self.config, "workeroutput", None)
        if workeroutput is not None:  # a pytest-xdist worker
            workeroutput[WORKER_KEY] = self.summary.dump()

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        dumped = getattr(node, "workeroutput", {}).get(WORKER_KEY)
        if dumped:
            self.summary.merge(dumped)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if hasattr(self.config, "workeroutput"):
            return
        write = terminalreporter.write_line
        terminalreporter.write_sep("=", "signal-guard summary")
        rows = self.summary.rows()
        if not rows:
            write("No reports.")
            return
        total = sum(row.count for row in rows)
        sites = _plural(len(rows), "call site")
        write(f"{total} {_plural(total, 'report')} at {len(rows)} {sites}.")
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


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"
