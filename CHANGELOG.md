# Changelog

## 0.2.0 (unreleased)

- System checks, tag `signals`:
  - W001: a module defines `@receiver` functions but is never imported.
  - W002: a receiver is connected only after startup, by a lazy import.
  - W003: a weakly connected function or lambda was garbage-collected.
  - W004: the same receiver is connected more than once.
- `manage.py signal_map`, with `--all`, `--write` and `--check` for a committed
  `signals.lock` file.
- A registry of `connect()` and `disconnect()` calls behind the checks.
- New settings `FIRST_PARTY` and `LOCK_FILE`.
- Changed: `CHECK_DJANGO_INTERNALS` is now `CHECK_LIBRARY_CALLS` and also skips
  bulk writes made inside installed packages, not only Django.
- Fixed: never ask a lazy object for its class while looking for signals.

## 0.1.0 (not published)

First release: the bypass guard.

- Wraps `QuerySet.bulk_create()`, `bulk_update()` and `update()` (and their
  async variants) and reports live `pre_save` / `post_save` receivers they
  would skip.
- Modes `raise`, `warn`, `log` and `off` via `SIGNAL_GUARD['BYPASS']`.
- `allow_bypass(*models, reason=...)` as a context manager, decorator and
  `async with`.
- `@bulk_safe` marker and `IGNORE_RECEIVERS` patterns.
- Bulk writes made by Django itself are skipped unless
  `CHECK_DJANGO_INTERNALS` is on (renamed in 0.2.0).
