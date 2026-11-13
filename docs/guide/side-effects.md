# Side effects before commit

```python
@receiver(post_save, sender=Order)
def send_confirmation(sender, instance, created, **kwargs):
    send_order_email.delay(instance.pk)  # the worker may run before COMMIT
```

Inside a transaction (`ATOMIC_REQUESTS`, `transaction.atomic()`, or Django's
own around `delete()`), a receiver runs **before the data is committed**. The
Celery worker looks for the order and gets `DoesNotExist`. The confirmation
email goes out even when the transaction rolls back.

django-signal-guard reports these calls when a receiver makes them inside a
transaction that has not committed:

| Side effect | Calls that are checked |
| --- | --- |
| Email | `send_mail()`, `EmailMessage.send()`, `mail_admins()`, `send_mass_mail()` |
| Celery | `Task.delay()`, `Task.apply_async()`, when Celery is installed |
| HTTP | `requests`, `httpx` (sync and async clients), when installed |
| Your own | Any function or method listed in `SIDE_EFFECT_CALLS` |

```text
SideEffectBeforeCommitError: Celery task shop.tasks.send_order_email is queued before the transaction on 'default' commits.
  receiver   shop.signals.send_confirmation  (shop/signals.py:30)
  called at  shop/signals.py:33
A worker can run it before the row is committed, and it still runs if the transaction rolls back.
Connect the receiver with @signal_guard.receiver_on_commit, or wrap the call in transaction.on_commit().
```

Set `SIGNAL_GUARD["SIDE_EFFECTS"]` to `"raise"`, `"warn"` (default), `"log"`
or `"off"`. `"raise"` stops the call before it happens, so nothing is sent.

Add your own side effects, such as a webhook client or a payment SDK:

```python
SIGNAL_GUARD = {
    "SIDE_EFFECT_CALLS": ["shop.webhooks.post", "shop.payments.Client.charge"],
}
```

A path that can't be imported fails at startup.

## Tests and transactions

The transaction that `TestCase` (and `pytest.mark.django_db`) wraps each test
in does not count: in production that code runs in autocommit mode. Your own
`transaction.atomic()` blocks inside a test do count, and so does the one
Django opens around `Model.delete()`.

## The fix: `@receiver_on_commit`

A drop-in replacement for `@receiver` that hands the call to
`transaction.on_commit()`:

```python
from signal_guard import receiver_on_commit


@receiver_on_commit(post_save, sender=Order)
def send_confirmation(sender, instance, created, **kwargs):
    if created:
        send_order_email.delay(instance.pk)  # the row is committed now
```

- The receiver gets **exactly the arguments the signal sent**.
- **Rolled back? It never runs.** Outside a transaction it runs at once.
- It uses the database the signal names in `using` (model signals do), or
  the `using=` you pass, or the default database.
- Takes `sender`, `weak`, `dispatch_uid`, a list of signals, and
  `robust=True` to log an exception instead of stopping the other commit
  callbacks.
- Returns your function unchanged, so you can still call it in tests.
  `signal_map` labels these receivers `on commit`.

## Tasks queued while a row is deleted

A `pre_delete` or `post_delete` receiver that queues
`cleanup.delay(instance.pk)` has a worse problem. Once the delete commits,
the row is gone, so a task that re-reads it with `.objects.get(pk=...)` finds
nothing and quietly does nothing. Moving the call to `on_commit()` doesn't
help, and the report says so:

```text
It runs during pre_delete for shop.Subscription #7. transaction.on_commit() alone will not fix it: after the commit the row is gone, so the task must not re-read it. Pass the task the values it needs, then queue it with on_commit().
```

```python
@receiver_on_commit(pre_delete, sender=Subscription)
def cancel_remote(sender, instance, **kwargs):
    cancel_remote_account.delay(external_id=instance.external_id)
```

## When the side effect must happen first: `allow_side_effect`

Sometimes the call has to happen inside the transaction, for example a
vendor's cancel API whose refusal must roll the local delete back. Allow it
and say why:

```python
from signal_guard import allow_side_effect


@receiver(pre_delete, sender=Subscription)
@allow_side_effect(reason="a refused cancel must roll the delete back")
def cancel_at_vendor(sender, instance, **kwargs):
    vendor.cancel(instance.vendor_id)
```

It also works as a context manager and with `async with`.

## Limits

- Only calls made while a **signal receiver** is running are checked. A view
  that sends email inside `atomic()` is out of scope.
- Calls are wrapped when Django starts. Code that ran
  `from django.core.mail import send_mass_mail` before then keeps the
  unwrapped function. `send_mail()` and `EmailMessage.send()` are always
  covered. The same goes for `SIDE_EFFECT_CALLS` functions imported by name
  before `signal_guard` is ready, so list functions that your receivers
  import in `ready()` or later.
- Receivers from installed packages are skipped unless `CHECK_LIBRARY_CALLS`
  is on; `IGNORE_RECEIVERS` patterns apply too.
- When your receiver calls a vendor SDK that makes the HTTP call, "called at"
  is the line in your receiver, not the line inside the SDK.

## Testing it

```python
from signal_guard.testing import assert_side_effect


def test_confirmation_waits_for_commit():
    with assert_side_effect("Celery task shop.tasks.*"), transaction.atomic():
        Order.objects.create()
```

See [Test assertions](testing.md).
