# pytest plugin and baseline

The plugin is installed with the package. Its
[fixtures](testing.md#pytest-fixtures) are always available. The two
features below do nothing until you ask for them, and both work with
pytest-xdist.

| Option | Ini setting | Effect |
| --- | --- | --- |
| `--signal-guard-summary` | | List every report at the end of the run, by call site. |
| `--signal-guard-baseline=FILE` | `signal_guard_baseline = FILE` | Silence the reports listed in FILE; fail the run on any other. |
| `--signal-guard-update-baseline` | | Silence every report, and write them all to FILE. |

## The summary

Run the suite with `--signal-guard-summary` to get every report grouped by
the line it blames, with a count and the tests that hit it:

```console
$ pytest --signal-guard-summary
...
============================= signal-guard summary =============================
4 reports at 2 call sites.

shop/imports.py:42  bypass  3 reports in 2 tests
    QuerySet.bulk_create() on shop.Order skips 2 receivers:
    - shop/tests/test_imports.py::test_import_rows
    - shop/tests/test_imports.py::test_reimport

shop/signals.py:33  side_effect  1 report in 1 test
    Celery task shop.tasks.send_order_email is queued before the transaction on 'default' commits.
    - shop/tests/test_views.py::test_checkout
```

Reports are counted in every mode. In `"raise"` mode a report also fails its
test, so for a first survey run in `"log"` mode, which lets the whole suite
reach the end:

```python
# settings/test.py
SIGNAL_GUARD = {"BYPASS": "log", "RECURSION": "log", "SIDE_EFFECTS": "log"}
```

## The baseline

On a codebase that already has hundreds of reports, you can't switch to
raise mode in one go. The baseline lets you start today: it lists the
reports you know about, and CI fails only on new ones.

**1. Write it** from a full run of the suite:

```bash
pytest --signal-guard-baseline=signal-guard-baseline.txt --signal-guard-update-baseline
```

While updating, every report is silenced, so the run goes through even in
raise mode. Commit the file:

```text
# django-signal-guard baseline: the reports known when it was written.
# These are not raised, warned or logged; any other report fails the run.
# One line per report: kind | path::function | what. Regenerate with:
#   pytest --signal-guard-baseline=signal-guard-baseline.txt --signal-guard-update-baseline
bypass | shop/imports.py::import_rows | shop.Order.bulk_create() skips shop.signals.normalize_status, shop.signals.reindex_order
side_effect | shop/signals.py::send_confirmation | shop.signals.send_confirmation: Celery task shop.tasks.send_order_email
```

**2. Use it** on every run, from the pytest configuration:

```toml
# pyproject.toml
[tool.pytest.ini_options]
signal_guard_baseline = "signal-guard-baseline.txt"
```

Now:

- **Known reports** are not raised, warned or logged.
- **A new report** behaves as its mode says: in raise mode it fails its test.
  In any mode, it also fails the run at the end:

```text
================ signal-guard: 1 new report not in signal-guard-baseline.txt ================

shop/views.py:88  bypass  1 report in 1 test
    QuerySet.update() on shop.Order skips 1 receiver:
    - shop/tests/test_views.py::test_cancel

Fix them, or if they are intended, wrap the call in allow_bypass() / allow_side_effect(), or regenerate the baseline with --signal-guard-update-baseline.
```

**3. Shrink it.** Fix a report, run the suite, and the summary says which
entries were not seen. Regenerate the file to drop them:

```text
1 baseline entry was not seen in this run: fixed, or not run. --signal-guard-update-baseline drops them.
```

Run the **whole suite** when you regenerate. Entries from tests that didn't
run are dropped too.

### What identifies a report

Each line is `kind | path::function | what`. There are no line numbers, so
editing a file doesn't invalidate its entries. A report stays known while
it comes from the same function and is about the same thing:

- **bypass**: the model, the method and the receivers skipped. A new
  receiver on the model is a new report.
- **side effect**: the receiver and the kind of call: the task name, the
  HTTP method and host, or "Email". Not the address or URL.
- **recursion**: the signal, the model and the receivers in the loop. Not
  the primary key.

The same identity is on every [`Report`](testing.md#capture_reports) as
`where` and `fingerprint`.

### Paths

`--signal-guard-baseline` is relative to the folder you run pytest from;
the ini setting is relative to the ini file. A missing file is an error,
with the command that creates it.
