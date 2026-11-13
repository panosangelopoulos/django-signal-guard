# Performance

django-signal-guard wraps calls your app makes all the time: every signal
sent, every bulk write. Each wrapper adds well under a microsecond in the
common case. A database write takes tens of microseconds even on SQLite in
memory, and far more over a network.

## Numbers

From `benchmarks/overhead.py` on an Apple M-series laptop, Python 3.14 and
Django 6.1. Nanoseconds per call, best of 15 runs:

| Case | Without | With | Added |
| --- | ---: | ---: | ---: |
| `Signal.send()`, no receivers | 132 | 205 | +73 |
| Model signal sent for a model without receivers | 231 | 411 | +180 |
| `Signal.send()`, one receiver | 764 | 1,266 | +502 |
| Bypass check, model without receivers | | 592 | +592 |
| Bypass check, `BYPASS = "off"` | | 540 | +540 |
| Bypass check, inside `allow_bypass()` | | 988 | +988 |
| Bypass check, receivers all `@bulk_safe` | | 2,197 | +2,197 |
| Side-effect call, outside a receiver | 60 | 438 | +378 |

For scale, one `Model.save()` or `QuerySet.update()` on SQLite in memory
takes about 50,000 ns on the same machine. Differences of a microsecond or
two between whole saves are within run-to-run noise, which is why the table
times each wrapper on its own.

## What costs what

- **Signal sends** pay for one entry on the tracing stack, and only when a
  receiver will run. The receiver is found by walking frames only when a
  recursion or side effect is reported.
- **Bulk writes** on a model without save receivers pay for two
  `has_listeners()` calls. With receivers, the guard also lists them; the
  `@bulk_safe` row is the full path that ends without a report.
- **Side-effect calls** check whether a receiver is running, and stop there
  when none is.
- **Reports** cost more: they walk frames and format a message. In `log`
  mode on a busy path, fix or allow the call rather than logging it
  thousands of times.

## Measure it yourself

```bash
uv run python benchmarks/overhead.py
uv run python benchmarks/overhead.py --json
```

Each case is timed with the wrappers installed and with Django's own methods
restored, taking turns, on the same objects. CI runs the benchmark on every
push and posts the table on the run's summary page. Shared runners are too
noisy for a pass/fail threshold, so CI reports the numbers but never fails
on them.
