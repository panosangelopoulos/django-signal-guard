import gc
import logging

import pytest
from django.core.management import call_command
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import Signal
from django.test import override_settings

from signal_guard import SignalBypassError, bulk_safe, receiver_on_commit
from signal_guard.checks import _identity, _receiver_functions, check_dead_receivers
from signal_guard.on_commit import DEFERRED_ATTR, is_on_commit
from tests.testapp.models import Invoice

UID = "test-on-commit"


class Calls(list):
    receiver = None


@pytest.fixture
def calls():
    """Connect an on-commit receiver for Invoice for one test; yield its calls."""
    received = Calls()
    try:

        @receiver_on_commit(post_save, sender=Invoice, dispatch_uid=UID)
        def record_invoice(sender, **kwargs):
            received.append((sender, kwargs))

        received.receiver = record_invoice
        yield received
    finally:
        post_save.disconnect(sender=Invoice, dispatch_uid=UID)


@pytest.mark.django_db
def test_runs_only_when_the_transaction_commits(
    calls, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        invoice = Invoice.objects.create()
        assert calls == []

    assert len(callbacks) == 1
    [(sender, kwargs)] = calls
    assert sender is Invoice
    assert kwargs["instance"] is invoice
    assert kwargs["created"] is True
    assert kwargs["signal"] is post_save
    assert kwargs["using"] == "default"


@pytest.mark.django_db(transaction=True)
def test_real_commit(calls):
    with transaction.atomic():
        Invoice.objects.create()
        assert calls == []
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_rollback_never_runs(calls):
    with pytest.raises(RuntimeError), transaction.atomic():
        Invoice.objects.create()
        raise RuntimeError("roll back")
    assert calls == []


@pytest.mark.django_db(transaction=True)
def test_autocommit_runs_at_once(calls):
    Invoice.objects.create()
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_robust_logs_and_keeps_going(caplog):
    ran = []

    @receiver_on_commit(post_save, sender=Invoice, robust=True, dispatch_uid="boom")
    def explode(sender, **kwargs):
        raise ValueError("boom")

    try:
        with (
            caplog.at_level(logging.ERROR, logger="django.db.backends.base"),
            transaction.atomic(),
        ):
            Invoice.objects.create()
            transaction.on_commit(lambda: ran.append(True))
    finally:
        post_save.disconnect(sender=Invoice, dispatch_uid="boom")

    assert ran == [True]
    assert any("boom" in r.getMessage() or r.exc_info for r in caplog.records)


@pytest.mark.django_db
def test_custom_signal_and_several_signals(django_capture_on_commit_callbacks):
    first, second = Signal(), Signal()
    received = []

    @receiver_on_commit([first, second], weak=False)
    def on_either(sender, signal, **kwargs):
        received.append((signal, kwargs))

    try:
        with django_capture_on_commit_callbacks(execute=True):
            first.send(sender=None, number=1)
            second.send(sender=None, number=2)
    finally:
        first.disconnect(on_either)
        second.disconnect(on_either)

    assert received == [(first, {"number": 1}), (second, {"number": 2})]


def test_returns_the_function_unchanged(calls):
    func = calls.receiver
    assert func.__name__ == "record_invoice"
    func(sender=Invoice, instance=None)
    assert calls == [(Invoice, {"instance": None})]
    deferred = getattr(func, DEFERRED_ATTR)
    assert is_on_commit(deferred)
    assert not is_on_commit(func)


def test_wrapper_lives_as_long_as_the_function(calls):
    gc.collect()
    assert post_save.has_listeners(Invoice)
    assert check_dead_receivers() == []


def test_duplicate_check_sees_the_function_not_the_wrapper(calls):
    deferred = getattr(calls.receiver, DEFERRED_ATTR)
    assert _identity(deferred) == _identity(calls.receiver)


@pytest.mark.django_db
def test_bypass_guard_names_the_function(calls):
    with pytest.raises(SignalBypassError) as caught:
        Invoice.objects.bulk_create([Invoice()])
    assert [r.path for r in caught.value.receivers] == [
        "tests.test_on_commit.calls.<locals>.record_invoice"
    ]
    assert "(tests/test_on_commit.py:" in str(caught.value)


@pytest.mark.django_db
@pytest.mark.parametrize("bulk_safe_outside", [True, False])
def test_bulk_safe_in_either_order(bulk_safe_outside):
    def handler(sender, **kwargs):
        pass

    if bulk_safe_outside:
        bulk_safe(
            receiver_on_commit(post_save, sender=Invoice, dispatch_uid="bs")(handler)
        )
    else:
        receiver_on_commit(post_save, sender=Invoice, dispatch_uid="bs")(
            bulk_safe(handler)
        )
    try:
        Invoice.objects.bulk_create([Invoice()])
    finally:
        post_save.disconnect(sender=Invoice, dispatch_uid="bs")


def test_signal_map_marks_on_commit_receivers(calls, capsys):
    with override_settings(SIGNAL_GUARD={"FIRST_PARTY": ["tests"]}):
        call_command("signal_map")
    out = capsys.readouterr().out
    assert "tests.test_on_commit.calls.<locals>.record_invoice  (" in out
    line = next(line for line in out.splitlines() if "record_invoice" in line)
    assert "uid=test-on-commit, on commit)" in line


def test_unimported_check_recognizes_the_decorator(tmp_path):
    module = tmp_path / "signals.py"
    module.write_text(
        "from signal_guard import receiver_on_commit\n"
        "import signal_guard\n\n"
        "@receiver_on_commit(post_save, sender=Order)\n"
        "def a(sender, **kwargs): ...\n\n"
        "@signal_guard.receiver_on_commit(post_save)\n"
        "def b(sender, **kwargs): ...\n"
    )
    assert _receiver_functions(str(module)) == ["a", "b"]
