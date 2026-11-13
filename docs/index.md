# django-signal-guard

**Make Django signal failures loud.**

Catch the bulk writes that silently skip your `pre_save` / `post_save`
receivers, the receivers that never get connected, run twice or loop forever,
and the email sent before the transaction commits.

## The problem

```python
Order.objects.bulk_create(rows)  # post_save never fires
Order.objects.filter(...).update(...)  # neither does pre_save
```

`bulk_create()`, `bulk_update()` and `update()` write rows **without calling
`Model.save()`**, so your `pre_save` and `post_save` receivers never run.

Nothing errors. The search index goes stale, the audit log has gaps, a cache is
never invalidated. You usually find out days later, in production.

## The fix

django-signal-guard catches those writes in development, tests and CI, and
tells you which receivers were skipped and where the call came from:

```text
SignalBypassError: QuerySet.bulk_create() on shop.Order skips 2 receivers:
  pre_save   shop.signals.normalize_status  (shop/signals.py:5)
  post_save  shop.signals.reindex_order  (shop/signals.py:9)
Called from shop/imports.py:42.
Run the receivers' work yourself, or wrap the call in signal_guard.allow_bypass(Order, reason="...").
```

It doesn't add new signals or replace them with hooks. Your signals stay
exactly as they are. It **reports misuse**, the way
[`nplusone`](https://github.com/jmcarp/nplusone) and
[`django-seal`](https://github.com/charettes/django-seal) report lazy queries.

## What's inside

| Feature | What it catches |
| --- | --- |
| [Bypass guard](guide/bypass.md) | Bulk writes that skip receivers: raise, warn or log. |
| [Recursion tracing](guide/recursion.md) | A receiver that saves its own instance and loops, with the whole chain. |
| [Side effects before commit](guide/side-effects.md) | Emails, Celery tasks and HTTP calls made before the transaction commits, and `@receiver_on_commit` to fix them. |
| [System checks](guide/system-checks.md) | Receivers that never run, only run in some processes, die silently, or run twice. |
| [`signals.lock`](guide/signal-map.md) | A committed inventory of every receiver, checked in CI, so new receivers show up in code review. |
| [Test assertions](guide/testing.md) | `assert_bypass()`, `assert_no_reports()`, `assert_sent()` and friends, for pytest and unittest. |
| [pytest plugin](guide/pytest.md) | A summary of every report by call site, and a baseline so CI fails only on new problems. |

## Next steps

- New here? Start with **[Getting started](getting-started.md)**.
- Adding it to a big, existing codebase? Read **[Large projects](guide/large-projects.md)**.
- Coming from 0.x? See **[Upgrading to 1.0](upgrading.md)**.

## Prior art

- **Johnny Metz's post** on blocking bulk operations that skip signals, which
  shows the same idea as a custom `QuerySet`.
- **`django-bulk-signals`, `django-bulk-tracker`** and similar packages add
  *new* bulk signals. Use them if you want receivers that do run on bulk
  writes; django-signal-guard tells you when your *existing* receivers don't.
- **`django-lifecycle`** replaces signals with model hooks.
