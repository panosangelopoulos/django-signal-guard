# Stability

From 1.0, django-signal-guard follows [semantic versioning](https://semver.org/).
A minor or patch release won't break code that uses the public API below.
Anything that would is announced with a deprecation warning at least one
minor release ahead, and only removed in the next major release.

## The public API

- Names in `signal_guard.__all__` and `signal_guard.testing.__all__`, with
  their documented arguments and attributes, including the attributes of the
  exceptions (`model`, `method`, `receivers`, `chain`, `call`, `receiver`)
  and of `Report`, `SentSignal`, `SkippedReceiver` and `Hop`.
- The keys and values of the `SIGNAL_GUARD` setting.
- The system check IDs `signal_guard.W001` to `W004`.
- The `signal_map` command, its options, and the `signals.lock` format.
- The pytest options, the ini setting, the fixtures `signal_reports` and
  `sent_signals`, and the baseline file format.
- The extra fields on log records.

## Not public

- Other modules and names, such as `signal_guard.guard.check_bypass` or
  `signal_guard.tracing.install`. They may change in any release.
- The exact wording of messages. Match on the exception's attributes, or a
  report's `kind`, `where` and `fingerprint`, not on its text.
- Which receivers the [create-only heuristic](guide/bypass.md#hints-in-the-report)
  recognises. It may learn new shapes.

## Changes in a minor release

A minor release may report a problem that an earlier release missed. In raise
mode that can fail a test that passed before. That is the package doing its
job, so it isn't counted as a breaking change. The
[baseline](guide/pytest.md#the-baseline) absorbs such reports when you
upgrade.

## Supported versions

| | Supported |
| --- | --- |
| Python | 3.10 to 3.14 |
| Django | 5.2 LTS, 6.0, 6.1 |

A Python or Django version is dropped only in a minor release, after its
upstream end of life, and is noted in the changelog. CI also runs against
Django's main branch.
