# Configuration

All settings live in one dict, and every key is optional:

```python
SIGNAL_GUARD = {
    "BYPASS": "warn",  # raise | warn | log | off
    "RECURSION": "warn",  # raise | warn | log | off
    "SIDE_EFFECTS": "warn",  # raise | warn | log | off
    "SIDE_EFFECT_CALLS": [],  # e.g. ["shop.webhooks.post"]
    "IGNORE_RECEIVERS": [],  # fnmatch patterns, e.g. ["simple_history.*"]
    "LIBRARY_RECEIVERS": "ignore_global",  # report | ignore_global | ignore
    "IGNORE_HISTORICAL_MODELS": True,
    "IGNORE_CREATE_ONLY_RECEIVERS": False,
    "IGNORE_RECEIVER_SELF_UPDATES": False,
    "CHECK_LIBRARY_CALLS": False,
    "FIRST_PARTY": None,  # e.g. ["shop", "billing"]
    "LOCK_FILE": "signals.lock",
    "ENABLED": True,
}
```

An unknown key or a bad value raises `ImproperlyConfigured` at startup, so a
typo fails fast.

Settings are read once, and read again whenever Django sends
`setting_changed` for `SIGNAL_GUARD`. `override_settings` and pytest-django's
`settings` fixture both do, so changing them in a test just works. Changing
the dict in place at run time does not.

## Modes

### `BYPASS`

Default `"warn"`. What to do when a bulk write would skip receivers. See
[Bypass guard](../guide/bypass.md).

| Value | Effect |
| --- | --- |
| `"raise"` | Raise `SignalBypassError` before the write reaches the database. |
| `"warn"` | Emit a `SignalBypassWarning` pointing at the call. |
| `"log"` | Log a warning on the `signal_guard` logger. |
| `"off"` | Do nothing. |

### `RECURSION`

Default `"warn"`. What to do when a receiver re-enters itself for the same
object twice. `"raise"` raises `SignalRecursionError` before the third run.
See [Recursion tracing](../guide/recursion.md).

### `SIDE_EFFECTS`

Default `"warn"`. What to do when a receiver sends email, queues a Celery
task or makes an HTTP call before the transaction commits. `"raise"` raises
`SideEffectBeforeCommitError` before the call happens. See
[Side effects before commit](../guide/side-effects.md).

## What is checked

### `SIDE_EFFECT_CALLS`

Default `[]`. Dotted paths of your own functions or methods that count as
side effects, such as `"shop.webhooks.post"` or
`"shop.payments.Client.charge"`. A path that cannot be imported fails at
startup.

### `IGNORE_RECEIVERS`

Default `[]`. Receivers whose dotted path matches one of these fnmatch
patterns are never reported, by any check. For third-party receivers you
can't change: `["simple_history.*"]`.

### `LIBRARY_RECEIVERS`

Default `"ignore_global"`. Which receivers from installed packages the
bypass guard reports.

| Value | Reported |
| --- | --- |
| `"report"` | Every library receiver. The 0.x default. |
| `"ignore_global"` | Library receivers connected to a specific model, not those connected for every sender (`sender=None`). |
| `"ignore"` | No library receivers. |

### `IGNORE_HISTORICAL_MODELS`

Default `True`. Skip bulk writes on the historical models that migrations
use. `False` was the 0.x default.

### `IGNORE_CREATE_ONLY_RECEIVERS`

Default `False`. For `update()` and `bulk_update()`, skip `post_save`
receivers that only act when `created`, judging by their source. A
heuristic; see [hints in the report](../guide/bypass.md#hints-in-the-report).

### `IGNORE_RECEIVER_SELF_UPDATES`

Default `False`. Skip bulk writes a `pre_save` or `post_save` receiver makes
to the model it is handling.

### `CHECK_LIBRARY_CALLS`

Default `False`. Also report bulk writes made inside Django or installed
packages, such as the `UPDATE` behind `on_delete=SET_NULL`, and side effects
in their receivers.

### `FIRST_PARTY`

Default `None`. Module prefixes of your own code, such as
`["shop", "billing"]`. By default, a module is yours unless it is Django,
this package, or installed in site-packages or the standard library.

### `LOCK_FILE`

Default `"signals.lock"`. Where `signal_map --write` and `--check` keep the
lock file. A relative path is resolved against `settings.BASE_DIR` when it
is set, else the current folder.

### `ENABLED`

Default `True`. When `False` at startup, nothing is wrapped and every check
does nothing.

## Log records

In `"log"` mode, reports go to the `signal_guard` logger at `WARNING`
level. Each record carries these extra fields, for your own handlers and log
pipeline:

| Field | Value |
| --- | --- |
| `signal_guard_kind` | `"bypass"`, `"side_effect"` or `"recursion"` |
| `signal_guard_location` | The blamed `path:line`. |
| `signal_guard_test` | The pytest node id, when a test is running. |
| `signal_guard_model` | Bypass: the model label, such as `"shop.Order"`. |
| `signal_guard_method` | Bypass: `"bulk_create"`, `"bulk_update"` or `"update"`. |
| `signal_guard_receivers` | Bypass: the skipped receivers' dotted paths. |
| `signal_guard_call` | Side effect: the call, such as `"Celery task shop.tasks.send"`. |
| `signal_guard_receiver` | Side effect: the receiver's dotted path. |
| `signal_guard_chain` | Recursion: the receivers along the loop. |

The record's `pathname` and `lineno` point at the blamed line, not at this
package.

## pytest

The pytest plugin takes the `signal_guard_baseline` ini setting and three
command line options. See [pytest plugin and baseline](../guide/pytest.md).
