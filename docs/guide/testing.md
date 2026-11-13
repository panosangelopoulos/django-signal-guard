# Test assertions

`signal_guard.testing` has assertions for two kinds of test:

- **about the guard's reports**: this import must not skip receivers; this
  job is meant to;
- **about signals**: saving an order sends `order_paid` with the right
  amount; this receiver is actually connected.

They are context managers, and work in pytest and unittest alike. Every
assertion is also a [pytest fixture](#pytest-fixtures) or a
[`unittest` method](#unittest).

## Reports

### `capture_reports()`

Collects reports instead of raising, warning or logging them, whatever the
mode:

```python
from signal_guard.testing import capture_reports


def test_import_reports(db):
    with capture_reports() as reports:
        import_orders(rows)
    assert reports.bypasses == []
    assert len(reports.side_effects) == 1
```

`reports` is a list of [`Report`](../reference/testing.md#signal_guard.testing.Report)
objects, with `.bypasses`, `.side_effects` and `.recursions` filters and an
`.assert_none()` method. Each report has:

| Attribute | Example |
| --- | --- |
| `kind` | `"bypass"`, `"side_effect"` or `"recursion"` |
| `message` | The full text, as raise mode would show it. |
| `location` | `"shop/imports.py:42"`, the line to blame. |
| `where` | `"shop/imports.py::import_orders"`, the same without the line number. |
| `fingerprint` | What went wrong, without line numbers, ids, addresses or URLs. |
| `error` | The exception raise mode raises, with structured details: `model`, `method` and `receivers` for a bypass; `call` and `receiver` for a side effect; `chain` for a recursion. |
| `test` | The pytest node id that was running, if any. |

Pass kinds to collect only those, and let the others be emitted as usual:
`capture_reports("bypass")`.

### `assert_no_reports()`

Fails if the block causes any report, listing every message:

```python
from signal_guard.testing import assert_no_reports


def test_import_is_clean(db):
    with assert_no_reports():
        import_orders(rows)
```

It works as a decorator too, and takes kinds:
`@assert_no_reports("side_effect")`.

### `assert_bypass()`, `assert_side_effect()`, `assert_recursion()`

Fail unless the block causes a matching report:

```python
from signal_guard.testing import assert_bypass, assert_recursion, assert_side_effect

with assert_bypass(Order, method="update", receivers=["*.reindex_order"]):
    Order.objects.update(indexed=False)

with assert_side_effect("Celery task shop.tasks.*", receiver="shop.signals.*"):
    with transaction.atomic():
        Order.objects.create()

with assert_recursion(receiver="shop.signals.sync_total"):
    Order.objects.create()
```

- `model` is a model class or an `"app_label.Model"` label.
- Strings that name receivers or calls are
  [fnmatch](https://docs.python.org/3/library/fnmatch.html) patterns.
- `count=` asks for exactly that many reports instead of at least one.
- Each yields the matching reports: `with assert_bypass(Order) as reports:`.

### How they treat other reports

- **Only what they expect is collected.** A report that doesn't match is
  emitted as usual, so in raise mode it raises inside the block. An
  assertion never hides a problem it wasn't looking for. When it fails, its
  message lists the reports of its kind that did happen.
- **Settings still apply.** A check set to `"off"` reports nothing, and an
  assertion waiting for it says so. `allow_bypass()`, `@bulk_safe` and
  `IGNORE_RECEIVERS` still apply too.
- **A recursion stops the block.** Letting a loop run would never end, so a
  collected recursion raises inside the block as raise mode would. The block
  ends there, and the error is swallowed.
- **Collected reports are not seen by the pytest plugin**: an expected
  bypass never counts as a [new report](pytest.md#the-baseline).

## Signals sent

### `capture_signals()`

Records the signals sent in the block, with their arguments, whether or not
anything receives them:

```python
from signal_guard.testing import capture_signals

with capture_signals(post_save, sender=Order) as sent:
    order.save()

assert sent[0].kwargs["created"] is False
assert sent[0].instance == order
```

Each [`SentSignal`](../reference/testing.md#signal_guard.tracing.SentSignal)
has `signal`, `signal_name`, `sender`, `kwargs`, `instance` and
`responses`, a list of `(receiver path, return value)`. With no signals it
records every signal; `sender=` limits it to one sender.

!!! note "What Django really sends"
    `Model.__init__` always sends `pre_init` and `post_init`, so capturing
    every signal shows those too. And `delete()` sends `pre_delete` and
    `post_delete` only when something receives them: without receivers,
    Django deletes in one query and sends nothing.

### `assert_sent()` and `assert_not_sent()`

```python
from signal_guard.testing import assert_not_sent, assert_sent

with assert_sent(order_paid, sender=Order, amount=10) as sent:
    order.pay(10)

with assert_not_sent(post_delete, sender=Order):
    order.archive()
```

Keyword arguments must equal the arguments the signal was sent with.
`assert_sent(..., count=2)` asks for exactly two sends. On failure, the
message lists what was sent instead.

## Receivers connected

```python
from signal_guard.testing import assert_connected, assert_not_connected

assert_connected(post_save, "shop.signals.reindex_order", sender=Order)
assert_not_connected(post_save, reindex_order, sender=Customer)
```

The receiver is the function itself or a pattern for its dotted path.
Receivers connected for every sender count. This catches the receiver whose
module is never imported, the [W001](system-checks.md) problem, from a test.

## pytest fixtures

Installed with the package:

```python
def test_import(signal_reports):
    import_orders(rows)
    assert signal_reports.bypasses == []


def test_saving_sends(sent_signals):
    Order.objects.create()
    assert "post_save" in [s.signal_name for s in sent_signals]
```

- `signal_reports` collects every report during the test, like
  `capture_reports()`.
- `sent_signals` records every signal sent during the test, like
  `capture_signals()`.

## unittest

```python
from django.test import TestCase
from signal_guard.testing import SignalGuardAssertionsMixin


class OrderTests(SignalGuardAssertionsMixin, TestCase):
    def test_import(self):
        with self.assertNoSignalReports():
            import_orders(rows)

    def test_pay(self):
        with self.assertSignalSent(order_paid, sender=Order):
            self.order.pay(10)
```

| Method | Same as |
| --- | --- |
| `assertNoSignalReports(*kinds)` | `assert_no_reports()` |
| `assertBypass(...)` | `assert_bypass()` |
| `assertSideEffect(...)` | `assert_side_effect()` |
| `assertRecursion(...)` | `assert_recursion()` |
| `assertSignalSent(...)` | `assert_sent()` |
| `assertSignalNotSent(...)` | `assert_not_sent()` |
| `assertReceiverConnected(...)` | `assert_connected()` |
| `assertReceiverNotConnected(...)` | `assert_not_connected()` |

Full signatures are in the [API reference](../reference/testing.md).
