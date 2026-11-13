# Large projects

On a big codebase the first run can report thousands of problems. Most are
noise you can switch off, and the rest can wait in a baseline while CI keeps
new ones out. This page is the path from "installed" to "raise mode in CI".

## 1. Survey in log mode

```python
# settings/test.py
SIGNAL_GUARD = {"BYPASS": "log", "RECURSION": "log", "SIDE_EFFECTS": "log"}
```

```bash
pytest --signal-guard-summary
```

The [summary](pytest.md#the-summary) lists every call site with its count
and tests. Read the top of it: a handful of call sites usually account for
most reports.

## 2. Cut the noise

The defaults already drop two kinds of report nobody can act on:

- **Library receivers connected for every model.** easy_thumbnails,
  imagekit and the like connect `pre_save` / `post_save` receivers with
  `sender=None`, so every `update()` on every model would skip them.
  [`LIBRARY_RECEIVERS = "ignore_global"`](bypass.md#receivers-from-installed-packages-library_receivers).
- **Data migrations.** Historical models never run your receivers.
  `IGNORE_HISTORICAL_MODELS = True`.

Two more are yours to judge:

```python
SIGNAL_GUARD = {
    # post_save receivers that start with `if not created: return`, or are
    # one `if created:` block, do nothing on update() and bulk_update().
    # A heuristic: it reads the source.
    "IGNORE_CREATE_ONLY_RECEIVERS": True,
    # A save receiver that writes to its own model with update(), on purpose,
    # so it doesn't run itself again. The write still skips the model's
    # other receivers.
    "IGNORE_RECEIVER_SELF_UPDATES": True,
}
```

Reports already say when these apply (`[create-only?]`, "runs inside"), so
the summary shows how much each would remove.

For specific receivers you can't change, use patterns:

```python
SIGNAL_GUARD = {"IGNORE_RECEIVERS": ["simple_history.*", "reversion.*"]}
```

And if your code doesn't all live outside site-packages, say which modules
are yours:

```python
SIGNAL_GUARD = {"FIRST_PARTY": ["shop", "billing", "accounts"]}
```

## 3. Mark what is intended

Some bulk writes skip receivers on purpose: an import that reindexes in one
batch, a nightly job. Say so in the code, so the next reader knows too:

```python
with allow_bypass(Order, reason="reindex_orders() runs after the import"):
    Order.objects.bulk_create(rows)
```

Mark receivers that never matter for bulk writes with
[`@bulk_safe`](bypass.md#receivers-that-never-matter-for-bulk-writes-bulk_safe),
and deliberate pre-commit side effects with
[`allow_side_effect()`](side-effects.md#when-the-side-effect-must-happen-first-allow_side_effect).

## 4. Baseline the rest, and switch to raise mode

```python
# settings/test.py
SIGNAL_GUARD = {"BYPASS": "raise", "RECURSION": "raise", "SIDE_EFFECTS": "raise"}
```

```bash
pytest --signal-guard-baseline=signal-guard-baseline.txt --signal-guard-update-baseline
```

```toml
# pyproject.toml
[tool.pytest.ini_options]
signal_guard_baseline = "signal-guard-baseline.txt"
```

From now on known reports are silent and new ones fail. See
[the baseline](pytest.md#the-baseline).

## 5. Pay it down

Each fixed entry shows up as "not seen" at the end of a run. Regenerate the
baseline after a full run to drop it, and watch the file shrink in code
review.

## Production

In production, keep the default `"warn"`, or use `"log"` to send reports to
your logging pipeline. Each log record carries
[structured fields](../reference/configuration.md#log-records) for
grouping. The overhead is small: see [Performance](../performance.md).
