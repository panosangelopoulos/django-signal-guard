# API

Everything here is importable from `signal_guard`. Test helpers are in
[`signal_guard.testing`](testing.md).

## Allowing what is intended

::: signal_guard.allow_bypass
    options:
      members: false

::: signal_guard.allow_side_effect
    options:
      members: false

::: signal_guard.bulk_safe

## Receivers

::: signal_guard.receiver_on_commit

## Exceptions and warnings

::: signal_guard.SignalGuardError

::: signal_guard.SignalBypassError

::: signal_guard.SignalBypassWarning

::: signal_guard.SignalRecursionError

::: signal_guard.SignalRecursionWarning

::: signal_guard.SideEffectBeforeCommitError

::: signal_guard.SideEffectBeforeCommitWarning

## Details carried by exceptions

::: signal_guard.SkippedReceiver
    options:
      show_root_full_path: false

::: signal_guard.tracing.Hop
