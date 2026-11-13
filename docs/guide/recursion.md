# Recursion tracing

A `post_save` receiver that calls `instance.save()` sends `post_save` again,
which calls the receiver again. You get a `RecursionError` a thousand frames
deep, or, worse, a loop that ends on its own after hammering the database.

django-signal-guard traces every signal being sent. When the same signal is
sent for the same object while it is already being handled twice, it reports
the loop:

```text
SignalRecursionError: shop.signals.sync_total re-entered for shop.Order #12:
  post_save  shop.Order #12 → shop.signals.sync_total  (shop/signals.py:20)
    Order.save()  shop/signals.py:24
  post_save  shop.Order #12 → shop.signals.sync_total  (shop/signals.py:20)
    Order.save()  shop/signals.py:24
  post_save  shop.Order #12  ← this send
Stop the loop with a guard (check update_fields or a flag on the instance), or write the change with QuerySet.update().
```

Each hop shows the signal, the object, the receiver that handled it, and the
line in that receiver that sent the next signal.

- **Saving once more is fine.** The common `if created: instance.save()`
  pattern re-enters once and stops, so it is never reported.
- **Same row, any object.** `Order.objects.get(pk=instance.pk).save()` inside
  the receiver is the same loop, and is caught.
- **Cycles too.** `Order` → `Invoice` → `Order` → … shows every hop.
- **Any signal.** Custom signals are traced as well as model signals.

Set `SIGNAL_GUARD["RECURSION"]` to `"raise"`, `"warn"` (default), `"log"`
or `"off"`. `"raise"` stops before the third run, so the loop never gets
going. In `"warn"` and `"log"` modes the loop is reported once and then runs
as it would without the guard.

## Fixing a loop

Guard the receiver:

```python
@receiver(post_save, sender=Order)
def sync_total(sender, instance, update_fields=None, **kwargs):
    if update_fields and set(update_fields) <= {"total"}:
        return  # this save is our own
    instance.total = instance.lines.aggregate(Sum("amount"))["amount__sum"]
    instance.save(update_fields=["total"])
```

Or write the change without sending signals:

```python
Order.objects.filter(pk=instance.pk).update(total=new_total)
```

The bypass guard will report that `update()`: either
[allow it](bypass.md#when-skipping-is-intended-allow_bypass), or set
[`IGNORE_RECEIVER_SELF_UPDATES`](bypass.md#hints-in-the-report).

## The exception

`SignalRecursionError.chain` holds one [`Hop`](../reference/api.md#signal_guard.tracing.Hop)
per signal sent, outermost first, for tests and tooling that want more than
the message.

## Testing it

Check that a fix holds, whatever the configured mode:

```python
from signal_guard.testing import assert_no_reports


def test_sync_total_does_not_loop():
    with assert_no_reports("recursion"):
        Order.objects.create()
```

`assert_recursion(receiver="shop.signals.sync_total")` asserts the opposite,
for code that is meant to be caught. It stops the loop, as raise mode would.
See [Test assertions](testing.md).
