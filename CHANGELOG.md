# Changelog

## Unreleased

Added, all off by default, to cut noise on large projects:

- `LIBRARY_RECEIVERS`: `"ignore_global"` stops the bypass guard reporting
  receivers from installed packages connected for every sender, such as
  easy_thumbnails' and imagekit's; `"ignore"` skips all library receivers.
- `IGNORE_HISTORICAL_MODELS`: skip bulk writes on the historical models
  migrations use.
- `IGNORE_CREATE_ONLY_RECEIVERS`: for `update()` and `bulk_update()`, skip
  `post_save` receivers whose source shows they only act when `created`. A
  heuristic.
- `IGNORE_RECEIVER_SELF_UPDATES`: skip bulk writes a `pre_save` or
  `post_save` receiver makes to its own model.

Also added:

- `allow_side_effect(reason=...)`, a context manager and decorator that
  allows side effects before commit, like `allow_bypass` does for bulk writes.
- A pytest plugin: `pytest --signal-guard-summary` lists reports by call
  site, with counts and the tests that hit them. Works with pytest-xdist.
- Log records carry `signal_guard_kind`, `signal_guard_location` and
  `signal_guard_test` (the running pytest node id).
- `SkippedReceiver` has `module`, `any_sender` and `create_only`.

Changed (report text only; what gets reported is unchanged unless noted):

- A task queued from a `pre_delete` or `post_delete` receiver is reported
  with advice that `on_commit()` alone won't help, since the row is gone by
  then.
- Bypass reports mark receivers that look create-only with `[create-only?]`,
  and say when the write runs inside a save receiver for the same model.
- A manager or queryset that overrides `update()`, `bulk_update()` or
  `bulk_create()` is no longer reported as the call site; the caller is, with
  the override after "through". When the override was in an installed
  package, these writes used to be skipped as library calls; they are now
  reported.
- Side-effect reports blame the first line of your own code inside the
  receiver, not a line in a vendor SDK it called.

Fixed:

- Paths in reports are relative to the project even when a file was reached
  through a symlink or another spelling of the folder, and relative to
  `settings.BASE_DIR` when the current folder is outside it.

## 0.3.0 (2026-11-13)

- Recursion tracing: a receiver that sends the same signal for the same
  object while it is already being handled twice is reported with the whole
  chain, such as a `post_save` receiver that calls `instance.save()`. New
  setting `RECURSION`, `SignalRecursionError` and `SignalRecursionWarning`.
- `@receiver_on_commit`: like `@receiver`, but runs the receiver through
  `transaction.on_commit()` with the arguments the signal sent.
- Side effects before commit: email, Celery tasks, `requests` and `httpx`
  calls made by a receiver inside a transaction that has not committed are
  reported. Transactions opened by `TestCase` do not count. New settings
  `SIDE_EFFECTS` and `SIDE_EFFECT_CALLS`, `SideEffectBeforeCommitError` and
  `SideEffectBeforeCommitWarning`.
- `@bulk_safe` works on either side of `@receiver_on_commit`; system checks
  W001 and W004, the registry and `signal_map` see through its wrapper.
- `IGNORE_RECEIVERS` and `CHECK_LIBRARY_CALLS` apply to the new checks.

## 0.2.0 (2026-11-13)

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
