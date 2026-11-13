<div align="center">

# 🛡️ django-signal-guard

**Make Django signal failures loud.**

Catch the bulk writes that silently skip your `pre_save` / `post_save` receivers,<br>
the receivers that never get connected, run twice, loop forever,<br>
or send the email before the transaction commits.

[![CI](https://github.com/panosangelopoulos/django-signal-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/panosangelopoulos/django-signal-guard/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-github.io-teal)](https://panosangelopoulos.github.io/django-signal-guard/)
[![Python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.14-3776AB?logo=python&logoColor=white)](#-install)
[![Django](https://img.shields.io/badge/django-5.2%20%7C%206.0%20%7C%206.1-0C4B33?logo=django&logoColor=white)](#-install)
[![PyPI](https://img.shields.io/pypi/v/django-signal-guard?label=pypi&color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/django-signal-guard/)
[![Downloads](https://img.shields.io/pepy/dt/django-signal-guard?label=downloads&color=blue)](https://pepy.tech/project/django-signal-guard)
[![Status](https://img.shields.io/badge/status-stable-brightgreen)](https://panosangelopoulos.github.io/django-signal-guard/stability/)
[![Typed](https://img.shields.io/badge/typing-typed-blue)](https://peps.python.org/pep-0561/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-green)](https://github.com/panosangelopoulos/django-signal-guard/blob/main/LICENSE)

**[Documentation](https://panosangelopoulos.github.io/django-signal-guard/)** •
[Getting started](https://panosangelopoulos.github.io/django-signal-guard/getting-started/) •
[Test assertions](https://panosangelopoulos.github.io/django-signal-guard/guide/testing/) •
[Large projects](https://panosangelopoulos.github.io/django-signal-guard/guide/large-projects/) •
[Configuration](https://panosangelopoulos.github.io/django-signal-guard/reference/configuration/) •
[Changelog](https://github.com/panosangelopoulos/django-signal-guard/blob/main/CHANGELOG.md)

</div>

---

## 💥 The problem

```python
Order.objects.bulk_create(rows)  # post_save never fires
Order.objects.filter(...).update(...)  # neither does pre_save
```

`bulk_create()`, `bulk_update()` and `update()` write rows **without calling
`Model.save()`**, so your `pre_save` and `post_save` receivers never run.

Nothing errors. The search index goes stale, the audit log has gaps, a cache is
never invalidated. You usually find out days later, in production.

## ✅ The fix

`django-signal-guard` catches those writes in development, tests and CI, and
tells you exactly which receivers were skipped and where the call came from:

```text
SignalBypassError: QuerySet.bulk_create() on shop.Order skips 2 receivers:
  pre_save   shop.signals.normalize_status  (shop/signals.py:5)
  post_save  shop.signals.reindex_order  (shop/signals.py:9)
Called from shop/imports.py:42.
Run the receivers' work yourself, or wrap the call in signal_guard.allow_bypass(Order, reason="...").
```

It doesn't add new signals or replace them with hooks. Your signals stay
exactly as they are. It just **reports misuse**, the way
[`nplusone`](https://github.com/jmcarp/nplusone) and
[`django-seal`](https://github.com/charettes/django-seal) report lazy queries.

## ✨ What's inside

| | Feature | What it catches |
| :---: | --- | --- |
| 🚧 | **[Bypass guard](https://panosangelopoulos.github.io/django-signal-guard/guide/bypass/)** | Bulk writes that skip receivers: raise, warn or log. |
| 🔁 | **[Recursion tracing](https://panosangelopoulos.github.io/django-signal-guard/guide/recursion/)** | A receiver that saves its own instance and loops, with the whole chain. |
| ⏳ | **[Before-commit check](https://panosangelopoulos.github.io/django-signal-guard/guide/side-effects/)** | Emails, Celery tasks and HTTP calls made before the transaction commits, and `@receiver_on_commit` to fix them. |
| 🩺 | **[System checks](https://panosangelopoulos.github.io/django-signal-guard/guide/system-checks/)** | Receivers that never run, only run in some processes, die silently, or run twice. |
| 🔒 | **[`signals.lock`](https://panosangelopoulos.github.io/django-signal-guard/guide/signal-map/)** | A committed inventory of every receiver, checked in CI, so new receivers show up in code review. |
| 🧪 | **[Test assertions](https://panosangelopoulos.github.io/django-signal-guard/guide/testing/)** | `assert_bypass()`, `assert_no_reports()`, `assert_sent()` and friends, for pytest and unittest. |
| 📋 | **[CI baseline](https://panosangelopoulos.github.io/django-signal-guard/guide/pytest/)** | Adopt it on a big codebase: known reports stay quiet, new ones fail CI. |

---

## 📦 Install

```bash
pip install django-signal-guard
```

Requires **Python 3.10+** and **Django 5.2+**.

## 🚀 Quick start

**1. Add it to `INSTALLED_APPS`, first in the list:**

```python
# settings.py
INSTALLED_APPS = [
    "signal_guard",  # first, so it sees every receiver being connected
    # ...
]
```

**2. Make every finding fail your tests:**

```python
# settings/test.py
SIGNAL_GUARD = {"BYPASS": "raise", "RECURSION": "raise", "SIDE_EFFECTS": "raise"}
```

**3. Add two lines to CI:**

```bash
python manage.py check --tag signals --fail-level WARNING
python manage.py signal_map --check
```

That's it. Everywhere else, the default is `"warn"`.

When skipping receivers is what you want, say so next to the code:

```python
from signal_guard import allow_bypass

with allow_bypass(Order, reason="import reindexes in one batch"):
    Order.objects.bulk_create(rows)
```

## 🧪 In your tests

```python
from signal_guard.testing import assert_bypass, assert_no_reports, assert_sent


def test_import_is_clean():
    with assert_no_reports():
        import_orders(rows)


def test_nightly_job_skips_reindex_on_purpose():
    with assert_bypass(Order, method="update", receivers=["*.reindex_order"]):
        mark_all_unindexed()


def test_paying_sends_order_paid():
    with assert_sent(order_paid, sender=Order, amount=10):
        order.pay(10)
```

Also as pytest fixtures (`signal_reports`, `sent_signals`) and as `unittest`
methods. See [Test assertions](https://panosangelopoulos.github.io/django-signal-guard/guide/testing/).

## 🏗️ On a big codebase

Thousands of reports on day one? Survey them, cut the noise, and let a
baseline keep the known ones quiet while CI fails on new ones:

```bash
pytest --signal-guard-summary                    # every report, by call site
pytest --signal-guard-baseline=signal-guard-baseline.txt --signal-guard-update-baseline
pytest --signal-guard-baseline=signal-guard-baseline.txt   # fails only on new reports
```

See [Large projects](https://panosangelopoulos.github.io/django-signal-guard/guide/large-projects/).

---

## 📚 Documentation

Everything else, from each check in detail to the configuration reference,
performance numbers and the stability policy, is at
**https://panosangelopoulos.github.io/django-signal-guard/**.

## 🧭 Prior art

- **Johnny Metz's post** on blocking bulk operations that skip signals, which
  shows the same idea as a custom `QuerySet`.
- **`django-bulk-signals`, `django-bulk-tracker`** and similar packages add
  *new* bulk signals. Use them if you want receivers that do run on bulk writes;
  `django-signal-guard` tells you when your *existing* receivers don't.
- **`django-lifecycle`** replaces signals with model hooks.

## 🧑‍💻 Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mkdocs serve                       # the documentation, live
uv run python benchmarks/overhead.py      # what each wrapper costs
```

Issues and pull requests are welcome.

## 📄 License

[BSD-3-Clause](https://github.com/panosangelopoulos/django-signal-guard/blob/main/LICENSE), the same as Django.
