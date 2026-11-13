# Upgrading to 1.0

Nothing in 1.0 breaks code written for 0.4. Two defaults changed, and both
only remove reports.

## Changed defaults

| Setting | 0.x | 1.0 | What you'll notice |
| --- | --- | --- | --- |
| `LIBRARY_RECEIVERS` | `"report"` | `"ignore_global"` | Library receivers connected for every model (easy_thumbnails, imagekit and the like) no longer appear in bypass reports. |
| `IGNORE_HISTORICAL_MODELS` | `False` | `True` | Bulk writes in data migrations are no longer checked. |

To keep the 0.x behaviour:

```python
SIGNAL_GUARD = {
    "LIBRARY_RECEIVERS": "report",
    "IGNORE_HISTORICAL_MODELS": False,
}
```

If you used `IGNORE_RECEIVERS` patterns only to hide receivers connected for
every model, you can probably drop them now.

## Settings are cached

0.x read `SIGNAL_GUARD` on every call. 1.0 reads it once, and again when
Django sends `setting_changed`. `override_settings` and pytest-django's
`settings` fixture send it, so tests behave as before. Code that changes
`settings.SIGNAL_GUARD` in place at run time, without either, will not be
seen. Use `override_settings` instead.

## New in 1.0

- [Test assertions](guide/testing.md) in `signal_guard.testing`.
- A [baseline](guide/pytest.md#the-baseline) for CI.
- A [performance](performance.md) benchmark, run in CI.
- This documentation.

The full list is in the [changelog](changelog.md).
