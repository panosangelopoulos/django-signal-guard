# System checks

```bash
python manage.py check --tag signals
```

| ID | Finds | Why it matters |
| --- | --- | --- |
| **`signal_guard.W001`** | A module in one of your apps defines `@receiver` functions, but nothing imports it. | The decorator never runs, so the receiver never runs. The classic *"my signal doesn't fire"*. |
| **`signal_guard.W002`** | A receiver is connected only after startup, by a lazy import such as `views.py` importing your signals module. | Web requests run it, but **Celery workers**, management commands and some tests never do. |
| **`signal_guard.W003`** | A weakly connected function or lambda was garbage-collected. | Django holds receivers by weak reference, so a nested function or lambda silently stops running. |
| **`signal_guard.W004`** | The same receiver is connected more than once to one signal and sender. | Every copy runs, so **emails go out twice**. |

Example:

```text
shop.webhooks: (signal_guard.W002) shop.webhooks.send_webhook is connected only after startup, when shop/views.py:2 imports it.
	HINT: Processes that never run that import, such as Celery workers, management commands and some tests, never run this receiver. Import shop.webhooks in shop.apps.ShopConfig.ready().
```

All four are warnings, so they never stop `runserver`. Fail CI on them with
`--fail-level WARNING`, and silence one with
`SILENCED_SYSTEM_CHECKS = ["signal_guard.W004"]`.

## How they work

- The checks import your URLconf first, as a running server would, then read
  Django's live receivers.
- **W001** parses your apps' modules (skipping tests and migrations) to find
  `@receiver` functions whose module was never imported.
- **W002** and **W003** rely on the registry of `connect()` calls that
  starts when `signal_guard` loads. That's why it should come **first** in
  `INSTALLED_APPS`.
- **W004** compares receivers by the file, line and name of their function,
  so it catches the same function connected under two `dispatch_uid`s, and
  a nested function connected once per call of its parent.

## In tests

`assert_connected()` makes the W001 question a test: is this receiver
actually connected?

```python
from signal_guard.testing import assert_connected


def test_reindex_is_wired_up():
    assert_connected(post_save, "shop.signals.reindex_order", sender=Order)
```

See [Test assertions](testing.md#receivers-connected).
