# Bypass guard

Every `bulk_create()`, `bulk_update()` and `update()` (plus `abulk_create()`,
`abulk_update()` and `aupdate()`) is checked against the live `pre_save` and
`post_save` receivers for the model. If any would be skipped, the guard
reports them, with the line that made the call:

```text
SignalBypassError: QuerySet.bulk_create() on shop.Order skips 2 receivers:
  pre_save   shop.signals.normalize_status  (shop/signals.py:5)
  post_save  shop.signals.reindex_order  (shop/signals.py:9)
Called from shop/imports.py:42.
Run the receivers' work yourself, or wrap the call in signal_guard.allow_bypass(Order, reason="...").
```

Set what happens with `SIGNAL_GUARD["BYPASS"]`:

| Mode | Effect |
| --- | --- |
| `"raise"` | `SignalBypassError`, **before anything reaches the database**. |
| `"warn"` (default) | A `SignalBypassWarning` pointing at the call. |
| `"log"` | A warning on the `signal_guard` logger. |
| `"off"` | Nothing. |

A model with no save receivers costs two `has_listeners()` calls; see
[Performance](../performance.md).

## When skipping is intended: `allow_bypass`

Wrap the call and say why. The `reason` is **required**, so the decision is
written down next to the code:

```python
from signal_guard import allow_bypass

with allow_bypass(Order, reason="import reindexes in one batch"):
    Order.objects.bulk_create(rows)
reindex_orders(rows)
```

- With no models, `allow_bypass(reason=...)` allows every model.
- It covers subclasses of the models you pass.
- It also works as a **decorator** and with **`async with`**.
- One instance can be used from several threads or tasks at once, for
  example as a decorator on a view.

## Receivers that never matter for bulk writes: `@bulk_safe`

Mark them once:

```python
from django.db.models.signals import post_save
from django.dispatch import receiver
from signal_guard import bulk_safe


@receiver(post_save, sender=Order)
@bulk_safe
def warm_cache(sender, instance, **kwargs): ...
```

The order of `@bulk_safe` and `@receiver` doesn't matter, and it works on
either side of [`@receiver_on_commit`](side-effects.md#the-fix-receiver_on_commit).

## Receivers you can't change: `IGNORE_RECEIVERS`

fnmatch patterns matched against receivers' dotted paths. Matching receivers
are never reported, by any check:

```python
SIGNAL_GUARD = {"IGNORE_RECEIVERS": ["simple_history.*", "imagekit.*"]}
```

## Receivers from installed packages: `LIBRARY_RECEIVERS`

Some packages connect receivers for **every** model (`sender=None`):
easy_thumbnails, imagekit and the like. Every `update()` on every model would
skip them. By default (`"ignore_global"`) those are not reported, while
library receivers connected to *your* models, such as `simple_history`'s,
still are.

| Value | Library receivers reported |
| --- | --- |
| `"report"` | All of them. |
| `"ignore_global"` (default) | Only those connected to a specific model. |
| `"ignore"` | None. |

What counts as a library follows [`FIRST_PARTY`](../reference/configuration.md#first_party).

## Who made the call

**Your code, not Django's.** Bulk writes made inside Django or an installed
package, such as the `UPDATE` behind `on_delete=SET_NULL`, are not reported:
you can't change that code. Set `CHECK_LIBRARY_CALLS` to report them too.

**Not your `update()` override.** A manager or queryset that overrides
`update()` (to set `updated_at`, say) and calls `super()` would be blamed for
every update in the project. The guard walks past it to the real caller, and
names the override after "through":

```text
Called from shop/views.py:42, through shop/managers.py:9 (TimestampedQuerySet.update()).
```

The same goes for `bulk_create()` and `bulk_update()` overrides, in your
code or a library's. A queryset method with another name, such as
`mark_paid()`, stays the call site.

## Hints in the report

Two patterns are usually harmless, so the report says when it sees them.

**Receivers that only act on create.** A `post_save` receiver that starts
with `if not created: return`, or whose body is one `if created:` block, does
nothing for `update()` and `bulk_update()`, which never create rows. Such
receivers are marked `[create-only?]`:

```text
QuerySet.update() on shop.Order skips 1 receiver:
  post_save  shop.signals.send_welcome  (shop/signals.py:30)  [create-only?]
...
The receiver marked [create-only?] seems to act only when created, judging by the source, and update() creates no rows. Set IGNORE_CREATE_ONLY_RECEIVERS to skip such receivers.
```

This is a **heuristic**: it reads the receiver's source and only knows those
two shapes. `IGNORE_CREATE_ONLY_RECEIVERS = True` drops such receivers from
`update()` and `bulk_update()` reports; `bulk_create()` still reports them.

**A receiver's own update.** A `post_save` receiver often writes to its own
model with `update()` on purpose, so that it doesn't run itself again:

```python
@receiver(post_save, sender=Order)
def stamp(sender, instance, **kwargs):
    Order.objects.filter(pk=instance.pk).update(stamped=True)
```

The report says it runs inside that receiver.
`IGNORE_RECEIVER_SELF_UPDATES = True` stops reporting these. Bear in mind
the write still skips the model's *other* receivers.

## Historical models

Data migrations work on the historical models Django builds from migration
state. Those never run your receivers, so by default
(`IGNORE_HISTORICAL_MODELS = True`) their bulk writes are not checked.

## What it covers, and what it doesn't

**Covered**

- `bulk_create()`, `bulk_update()` and `update()`, called on a manager, a
  queryset, or your own `QuerySet` subclass methods.
- The async variants `abulk_create()`, `abulk_update()` and `aupdate()`.
- Receivers connected to the model, and receivers connected to every sender.
- `bulk_update()` is reported once, even though Django runs `update()`
  inside it.

**Not covered**

- Raw SQL, `cursor.execute()`, database triggers and other writes that never
  go through a `QuerySet`.
- `QuerySet.delete()`. It *does* send `pre_delete` and `post_delete`; it only
  skips your model's own `delete()` method.
- Logic in an overridden `Model.save()` with no signal receiver. The guard
  only knows about receivers.
- A `QuerySet` subclass that overrides one of the three methods and never
  calls `super()`.

## Testing it

```python
from signal_guard.testing import assert_bypass


def test_bulk_import_skips_reindex():
    with assert_bypass(Order, method="bulk_create", receivers=["*.reindex_order"]):
        import_orders(rows)
```

See [Test assertions](testing.md).
