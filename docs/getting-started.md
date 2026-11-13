# Getting started

## Install

```bash
pip install django-signal-guard
```

Requires **Python 3.10+** and **Django 5.2+**.

!!! tip "With uv"
    Add it to the project with `uv add --dev django-signal-guard` rather than
    trying it with `uv run --with django-signal-guard`. `--with` resolves the
    package's dependencies on their own, so it can install a different Django
    from the one your project pins.

## Set up

**1. Add it to `INSTALLED_APPS`, first in the list:**

```python
# settings.py
INSTALLED_APPS = [
    "signal_guard",  # first, so it sees every receiver being connected
    # ...
]
```

It has to come first because it records each `Signal.connect()` call from
the moment it loads. A receiver connected before that has no record, so the
[system checks](guide/system-checks.md) know less about it.

**2. Make every finding fail your tests:**

```python
# settings/test.py
SIGNAL_GUARD = {"BYPASS": "raise", "RECURSION": "raise", "SIDE_EFFECTS": "raise"}
```

Everywhere else the default is `"warn"`: a Python warning, which
`runserver` prints and production usually ignores. Use `"log"` to send
reports to the `signal_guard` logger instead, or `"off"`.

**3. Add two lines to CI:**

```bash
python manage.py check --tag signals --fail-level WARNING
python manage.py signal_map --check
```

The first runs the [system checks](guide/system-checks.md). The second
compares the receivers connected now with your committed
[`signals.lock`](guide/signal-map.md); create it once with
`python manage.py signal_map --write`.

That's it.

## What you'll see

A bulk write that skips receivers, in raise mode:

```text
SignalBypassError: QuerySet.update() on shop.Order skips 1 receiver:
  post_save  shop.signals.reindex_order  (shop/signals.py:9)
Called from shop/views.py:42.
Run the receivers' work yourself, or wrap the call in signal_guard.allow_bypass(Order, reason="...").
```

When skipping is what you want, say so next to the code:

```python
from signal_guard import allow_bypass

with allow_bypass(Order, reason="the nightly job reindexes everything"):
    Order.objects.update(indexed=False)
```

## Next steps

- Each check in detail: [bypass guard](guide/bypass.md),
  [recursion](guide/recursion.md),
  [side effects before commit](guide/side-effects.md).
- Write tests about signals: [test assertions](guide/testing.md).
- Thousands of reports on day one? Read [Large projects](guide/large-projects.md):
  it shows how to cut the noise and adopt raise mode gradually with a
  [baseline](guide/pytest.md#the-baseline).
