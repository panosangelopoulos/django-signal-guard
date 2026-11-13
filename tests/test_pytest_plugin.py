"""The pytest plugin: --signal-guard-summary groups reports by call site."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from signal_guard.pytest_plugin import Summary
from signal_guard.reporting import Report

ROOT = Path(__file__).resolve().parent.parent

INNER_TESTS = """
import pytest
from django.test import override_settings

from tests.testapp.models import Order

pytestmark = pytest.mark.django_db


def bulk():
    Order.objects.bulk_create([Order()])  # SITE_A


@override_settings(SIGNAL_GUARD={"BYPASS": "log"})
def test_one():
    bulk()
    bulk()
    Order.objects.update(status="x")  # SITE_B


@override_settings(SIGNAL_GUARD={"BYPASS": "log"})
def test_two():
    bulk()


def test_quiet():
    pass
"""


def run_inner(tmp_path, *args, source=INNER_TESTS):
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(textwrap.dedent(source))
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings",
        "PYTHONPATH": str(ROOT),
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            *([] if any(a.startswith("-c") for a in args) else ["-c", os.devnull]),
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def line_of(marker):
    for number, line in enumerate(textwrap.dedent(INNER_TESTS).splitlines(), 1):
        if marker in line:
            return number
    raise AssertionError(marker)


def test_summary_groups_reports_by_call_site(tmp_path):
    result = run_inner(tmp_path, "--signal-guard-summary")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "signal-guard summary" in out
    site_a = f"test_inner.py:{line_of('SITE_A')}"
    site_b = f"test_inner.py:{line_of('SITE_B')}"
    lines = out.splitlines()
    [a] = [line for line in lines if site_a in line]
    [b] = [line for line in lines if site_b in line]
    assert "3 reports in 2 tests" in a
    assert "1 report in 1 test" in b
    assert lines.index(a) < lines.index(b)  # most reports first
    assert "test_inner.py::test_one" in out
    assert "QuerySet.bulk_create() on testapp.Order skips 2 receivers" in out


def test_no_summary_without_the_flag(tmp_path):
    result = run_inner(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "signal-guard summary" not in result.stdout


def report(location, test, kind="bypass"):
    return Report(kind=kind, location=location, message="first\nsecond", test=test)


def test_summary_merges_worker_output():
    # Under pytest-xdist each worker collects its own reports.
    one, two, main = Summary(), Summary(), Summary()
    one.record(report("a.py:1", "t1"))
    two.record(report("a.py:1", "t2"))
    two.record(report("b.py:2", "t2", kind="side_effect"))
    main.merge(one.dump())
    main.merge(two.dump())

    rows = main.rows()
    assert [(r.kind, r.location, r.count, len(r.tests)) for r in rows] == [
        ("bypass", "a.py:1", 2, 2),
        ("side_effect", "b.py:2", 1, 1),
    ]
    assert rows[0].headline == "first"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tests/test_x.py::test_a (call)", "tests/test_x.py::test_a"),
        ("tests/test_x.py::test_a[1 (x)] (setup)", "tests/test_x.py::test_a[1 (x)]"),
        (None, None),
    ],
)
def test_current_test(monkeypatch, raw, expected):
    from signal_guard.reporting import current_test

    if raw is None:
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    else:
        monkeypatch.setenv("PYTEST_CURRENT_TEST", raw)
    assert current_test() == expected


# --- the baseline -----------------------------------------------------------

BASELINE_TESTS = """
import pytest
from django.test import override_settings

from tests.testapp.models import Order

pytestmark = pytest.mark.django_db


def test_raise_mode():
    Order.objects.update(status="x")  # raises unless in the baseline


@override_settings(SIGNAL_GUARD={"BYPASS": "log"})
def test_log_mode():
    Order.objects.bulk_create([Order()])
"""

NEW_RAISE = """

def test_new_raise():
    Order.objects.filter(pk=1).update(status="y")
"""

NEW_LOG = """

@override_settings(SIGNAL_GUARD={"BYPASS": "log"})
def test_new_log():
    Order.objects.filter(pk=1).update(status="y")
"""


def write_baseline(tmp_path):
    result = run_inner(
        tmp_path,
        "--signal-guard-baseline=baseline.txt",
        "--signal-guard-update-baseline",
        source=BASELINE_TESTS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return (tmp_path / "baseline.txt").read_text()


def test_update_baseline_writes_every_report_and_lets_the_run_pass(tmp_path):
    text = write_baseline(tmp_path)
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == [
        "bypass | test_inner.py::test_log_mode | testapp.Order.bulk_create() skips "
        "tests.testapp.signals.normalize_status, tests.testapp.signals.reindex_order",
        "bypass | test_inner.py::test_raise_mode | testapp.Order.update() skips "
        "tests.testapp.signals.normalize_status, tests.testapp.signals.reindex_order",
    ]
    assert "--signal-guard-update-baseline" in text  # how to regenerate


def test_known_reports_are_silenced(tmp_path):
    write_baseline(tmp_path)
    result = run_inner(
        tmp_path, "--signal-guard-baseline=baseline.txt", source=BASELINE_TESTS
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no new reports" in result.stdout


def test_new_report_in_raise_mode_fails_its_test(tmp_path):
    write_baseline(tmp_path)
    result = run_inner(
        tmp_path,
        "--signal-guard-baseline=baseline.txt",
        source=BASELINE_TESTS + NEW_RAISE,
    )
    assert result.returncode == 1
    assert "1 failed, 2 passed" in result.stdout
    assert "1 new report not in baseline.txt" in result.stdout
    assert "test_inner.py::test_new_raise" in result.stdout


def test_new_report_in_log_mode_fails_the_session(tmp_path):
    write_baseline(tmp_path)
    result = run_inner(
        tmp_path,
        "--signal-guard-baseline=baseline.txt",
        source=BASELINE_TESTS + NEW_LOG,
    )
    # Every test passed, but the run fails on the new report.
    assert "3 passed" in result.stdout
    assert result.returncode == 1
    assert "1 new report not in baseline.txt" in result.stdout


def test_entries_not_seen_are_listed(tmp_path):
    write_baseline(tmp_path)
    result = run_inner(
        tmp_path,
        "--signal-guard-baseline=baseline.txt",
        "-k",
        "raise_mode",
        source=BASELINE_TESTS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 baseline entry was not seen" in result.stdout


def test_baseline_from_ini(tmp_path):
    write_baseline(tmp_path)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nsignal_guard_baseline = baseline.txt\n"
    )
    result = run_inner(tmp_path, "-c", "pytest.ini", source=BASELINE_TESTS + NEW_LOG)
    assert result.returncode == 1
    assert "1 new report not in baseline.txt" in result.stdout


def test_missing_baseline_file_is_an_error(tmp_path):
    result = run_inner(
        tmp_path, "--signal-guard-baseline=missing.txt", source=BASELINE_TESTS
    )
    assert result.returncode != 0
    assert "missing.txt" in result.stderr + result.stdout
    assert "--signal-guard-update-baseline" in result.stderr + result.stdout


def test_summary_and_baseline_under_xdist(tmp_path):
    pytest.importorskip("xdist")
    write_baseline(tmp_path)
    result = run_inner(
        tmp_path,
        "-n",
        "2",
        "--signal-guard-summary",
        "--signal-guard-baseline=baseline.txt",
        source=BASELINE_TESTS + NEW_LOG,
    )
    out = result.stdout
    assert result.returncode == 1, out + result.stderr
    assert "3 reports at 3 call sites." in out
    assert "1 new report not in baseline.txt" in out
    assert "test_inner.py::test_new_log" in out
    assert "not seen" not in out  # both workers' reports reached the controller
