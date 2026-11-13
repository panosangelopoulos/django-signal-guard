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


def run_inner(tmp_path, *args):
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(textwrap.dedent(INNER_TESTS))
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
            "-c",
            os.devnull,
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
