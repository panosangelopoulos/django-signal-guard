"""Measure what django-signal-guard adds to each call it wraps.

Run from the repository root::

    uv run python benchmarks/overhead.py            # a Markdown table
    uv run python benchmarks/overhead.py --json     # the same numbers as JSON

Each case is timed with the package's wrappers installed and with the
original Django methods restored, on the same objects. Times are the best of
several runs, in nanoseconds per call, so they show the cost of the code
itself rather than of whatever else the machine was doing.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import timeit
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import django  # noqa: E402
from django.conf import settings  # noqa: E402

settings.configure(
    SECRET_KEY="benchmark",
    USE_TZ=True,
    DEFAULT_AUTO_FIELD="django.db.models.AutoField",
    INSTALLED_APPS=[
        "signal_guard",
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "tests.testapp",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    SIGNAL_GUARD={"BYPASS": "raise", "RECURSION": "raise", "SIDE_EFFECTS": "raise"},
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
)
django.setup()

from django.core.management import call_command  # noqa: E402
from django.dispatch import Signal  # noqa: E402
from django.test.utils import override_settings  # noqa: E402

from signal_guard import allow_bypass, guard, side_effects, tracing  # noqa: E402
from signal_guard.guard import check_bypass  # noqa: E402
from tests.testapp import webhooks  # noqa: E402
from tests.testapp.models import Note, Order, Tag  # noqa: E402

call_command("migrate", run_syncdb=True, verbosity=0)

REPEAT = 15


def best_ns(func: Callable[[], object], number: int) -> float:
    """The fastest of REPEAT runs, in nanoseconds per call."""
    func()  # warm caches
    runs = timeit.repeat(func, number=number, repeat=REPEAT)
    return min(runs) / number * 1e9


def compare(
    install: Callable[[], None],
    uninstall: Callable[[], None],
    func: Callable[[], object],
    number: int,
) -> tuple[float, float]:
    """(time without the wrapper, time with it).

    The two are timed in turn within each run, so a machine that speeds up
    or slows down along the way affects both alike.
    """
    func()  # warm caches
    without, with_ = [], []
    try:
        for _ in range(REPEAT):
            uninstall()
            without.append(timeit.timeit(func, number=number))
            install()
            with_.append(timeit.timeit(func, number=number))
    finally:
        install()
    return min(without) / number * 1e9, min(with_) / number * 1e9


def receiver(sender: object, **kwargs: object) -> None:
    pass


def main() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    def add(case: str, without: float, with_: float) -> None:
        rows.append(
            {
                "case": case,
                "without_ns": round(without),
                "with_ns": round(with_),
                "added_ns": round(with_ - without),
            }
        )

    # --- signal dispatch (recursion tracing) ---
    empty = Signal()
    one = Signal()
    one.connect(receiver, weak=False)
    add(
        "Signal.send(), no receivers",
        *compare(tracing.install, tracing.uninstall, lambda: empty.send(None), 200_000),
    )
    # Caching per sender, as model signals such as pre_save do.
    other = Signal(use_caching=True)
    other.connect(receiver, sender=Order, weak=False)
    add(
        "model signal sent for a model without receivers",
        *compare(tracing.install, tracing.uninstall, lambda: other.send(Tag), 200_000),
    )
    add(
        "Signal.send(), one receiver",
        *compare(tracing.install, tracing.uninstall, lambda: one.send(None), 200_000),
    )

    tag = Tag.objects.create(name="a")
    add(
        "Model.save(), for scale (sqlite in memory, noisy)",
        *compare(tracing.install, tracing.uninstall, tag.save, 5_000),
    )

    # --- the bypass guard ---
    def update_tag() -> object:
        return Tag.objects.filter(pk=tag.pk).update(name="b")

    add(
        "QuerySet.update(), for scale (sqlite in memory, noisy)",
        *compare(guard.install, guard.uninstall, update_tag, 5_000),
    )
    add(
        "bypass check, model without receivers",
        0,
        best_ns(lambda: check_bypass(Tag, "update"), 200_000),
    )
    add(
        "bypass check, receivers all @bulk_safe",
        0,
        best_ns(lambda: check_bypass(Note, "update"), 50_000),
    )
    with allow_bypass(Order, reason="benchmark"):
        add(
            "bypass check, inside allow_bypass()",
            0,
            best_ns(lambda: check_bypass(Order, "update"), 50_000),
        )
    with override_settings(SIGNAL_GUARD={"BYPASS": "off"}):
        add(
            "bypass check, BYPASS = 'off'",
            0,
            best_ns(lambda: check_bypass(Order, "update"), 50_000),
        )

    # --- side effects ---
    side_effects.install(("tests.testapp.webhooks.post",))

    def post() -> None:
        webhooks.post("https://example.com")
        webhooks.CALLS.clear()

    add(
        "side-effect call, outside a receiver",
        *compare(
            lambda: side_effects.install(("tests.testapp.webhooks.post",)),
            side_effects.uninstall,
            post,
            200_000,
        ),
    )
    return rows


def as_markdown(rows: list[dict[str, object]]) -> str:
    lines = [
        f"Python {platform.python_version()}, Django {django.get_version()}, "
        f"{platform.machine()} {platform.system()}. Nanoseconds per call, best "
        f"of {REPEAT} runs.",
        "",
        "| Case | Without | With | Added |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in rows:
        without = f"{row['without_ns']:,}" if row["without_ns"] else "—"
        lines.append(
            f"| {row['case']} | {without} | {row['with_ns']:,} | {row['added_ns']:+,} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print JSON")
    options = parser.parse_args()
    results = main()
    print(json.dumps(results, indent=2) if options.json else as_markdown(results))
