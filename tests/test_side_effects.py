import logging
import sys
import warnings

import httpx
import pytest
import requests
from asgiref.sync import async_to_sync, sync_to_async
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail, send_mass_mail
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete
from django.test import override_settings

from signal_guard import (
    SideEffectBeforeCommitError,
    SideEffectBeforeCommitWarning,
    allow_side_effect,
    receiver_on_commit,
    side_effects,
)
from signal_guard.side_effects import TASK, Target, _celery, uncommitted_alias
from tests.fakevendor import sdk
from tests.testapp import webhooks
from tests.testapp.models import Invoice

pytestmark = pytest.mark.django_db

THIS_FILE = "tests/test_side_effects.py"
RAISE = override_settings(SIGNAL_GUARD={"SIDE_EFFECTS": "raise"})


def email_customer(sender, instance, **kwargs):
    send_mail("Thanks", "Your invoice", None, [instance.email])


@pytest.fixture
def extra_calls():
    """Wrap extra SIDE_EFFECT_CALLS for one test, then restore them."""
    before = len(side_effects._PATCHED)

    def _install(*paths):
        side_effects.install(paths)

    yield _install
    while len(side_effects._PATCHED) > before:
        owner, attr, raw = side_effects._PATCHED.pop()
        setattr(owner, attr, raw)
    webhooks.CALLS.clear()


# --- when it reports --------------------------------------------------------


@RAISE
def test_email_in_receiver_inside_transaction_raises(connect):
    connect(post_save, email_customer, sender=Invoice)

    with pytest.raises(SideEffectBeforeCommitError) as caught, transaction.atomic():
        Invoice.objects.create()

    assert mail.outbox == []
    assert caught.value.call == "Email to customer@example.com"
    assert caught.value.receiver == "tests.test_side_effects.email_customer"


@RAISE
def test_message(connect):
    connect(post_save, email_customer, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError) as caught, transaction.atomic():
        Invoice.objects.create()

    lines = str(caught.value).splitlines()
    assert lines[0] == (
        "Email to customer@example.com is sent before the transaction on "
        "'default' commits."
    )
    assert lines[1].startswith(
        f"  receiver   tests.test_side_effects.email_customer  ({THIS_FILE}:"
    )
    assert lines[2].startswith(f"  called at  {THIS_FILE}:")
    assert "rolls back" in lines[3]
    assert "@signal_guard.receiver_on_commit" in lines[4]


@RAISE
def test_test_case_transaction_does_not_count(connect):
    # pytest-django wraps this test in a transaction, as TestCase does. In
    # production this save would run in autocommit mode.
    connect(post_save, email_customer, sender=Invoice)
    assert uncommitted_alias() is None
    Invoice.objects.create()
    assert len(mail.outbox) == 1


@RAISE
@pytest.mark.django_db(transaction=True)
def test_autocommit_does_not_count(connect):
    connect(post_save, email_customer, sender=Invoice)
    Invoice.objects.create()
    assert len(mail.outbox) == 1


@RAISE
def test_outside_receivers_is_not_reported():
    with transaction.atomic():
        send_mail("Hi", "body", None, ["a@example.com"])
    assert len(mail.outbox) == 1


@RAISE
def test_delete_signals_run_inside_djangos_own_transaction(connect):
    # Collector.delete() sends post_delete inside transaction.atomic().
    invoice = Invoice.objects.create()
    connect(post_delete, email_customer, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError):
        invoice.delete()


@RAISE
def test_receiver_on_commit_is_the_fix(django_capture_on_commit_callbacks):
    @receiver_on_commit(post_save, sender=Invoice, dispatch_uid="email-on-commit")
    def email_after_commit(sender, instance, **kwargs):
        send_mail("Thanks", "Your invoice", None, [instance.email])

    try:
        with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
            Invoice.objects.create()
            assert mail.outbox == []
    finally:
        post_save.disconnect(sender=Invoice, dispatch_uid="email-on-commit")
    assert len(mail.outbox) == 1


@RAISE
def test_send_mass_mail(connect):
    def mass(sender, instance, **kwargs):
        send_mass_mail([("s", "m", None, [instance.email])])

    connect(post_save, mass, sender=Invoice)
    with (
        pytest.raises(SideEffectBeforeCommitError, match="send_mass_mail"),
        transaction.atomic(),
    ):
        Invoice.objects.create()


# --- libraries --------------------------------------------------------------


@RAISE
def test_requests(connect):
    def call_webhook(sender, instance, **kwargs):
        requests.post("https://hooks.example.com/invoice", json={"id": instance.pk})

    connect(post_save, call_webhook, sender=Invoice)
    # Raised before the request is made, so nothing reaches the network.
    with pytest.raises(SideEffectBeforeCommitError) as caught, transaction.atomic():
        Invoice.objects.create()
    assert caught.value.call == "HTTP POST https://hooks.example.com/invoice"
    assert f"called at  {THIS_FILE}:" in str(caught.value)


def _mock_client(cls):
    return cls(transport=httpx.MockTransport(lambda request: httpx.Response(204)))


@RAISE
def test_httpx(connect):
    def call_webhook(sender, instance, **kwargs):
        with _mock_client(httpx.Client) as client:
            client.post("https://hooks.example.com/invoice")

    connect(post_save, call_webhook, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError) as caught, transaction.atomic():
        Invoice.objects.create()
    assert caught.value.call == "HTTP POST https://hooks.example.com/invoice"


@RAISE
def test_httpx_outside_a_transaction_still_works(connect):
    responses = []

    def call_webhook(sender, instance, **kwargs):
        with _mock_client(httpx.Client) as client:
            responses.append(client.get("https://hooks.example.com/").status_code)

    connect(post_save, call_webhook, sender=Invoice)
    Invoice.objects.create()
    assert responses == [204]


def test_httpx_async_client_is_wrapped():
    async def fetch():
        async with _mock_client(httpx.AsyncClient) as client:
            return (await client.get("https://hooks.example.com/")).status_code

    assert hasattr(httpx.AsyncClient.send, side_effects.ORIGINAL_ATTR)
    assert async_to_sync(fetch)() == 204


@pytest.fixture
def fake_celery(tmp_path, monkeypatch):
    """A tiny stand-in for celery.app.task, which is all the check wraps."""
    package = tmp_path / "celery"
    (package / "app").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "app" / "__init__.py").write_text("")
    (package / "app" / "task.py").write_text(
        "class Task:\n"
        "    name = 'shop.tasks.send_receipt'\n"
        "    queued = []\n"
        "    def apply_async(self, args=None, kwargs=None, **options):\n"
        "        self.queued.append(args)\n"
        "    def delay(self, *args, **kwargs):\n"
        "        return self.apply_async(args, kwargs)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    before = len(side_effects._PATCHED)
    side_effects._patch(
        Target("celery.app.task.Task.apply_async", TASK, _celery, optional=True)
    )
    from celery.app.task import Task

    yield Task
    while len(side_effects._PATCHED) > before:
        owner, attr, raw = side_effects._PATCHED.pop()
        setattr(owner, attr, raw)
    for name in [m for m in sys.modules if m == "celery" or m.startswith("celery.")]:
        del sys.modules[name]


@RAISE
def test_celery_task(connect, fake_celery):
    def queue_receipt(sender, instance, **kwargs):
        fake_celery().delay(instance.pk)

    connect(post_save, queue_receipt, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError) as caught, transaction.atomic():
        Invoice.objects.create()
    assert caught.value.call == "Celery task shop.tasks.send_receipt"
    assert "is queued before the transaction" in str(caught.value)
    assert fake_celery.queued == []


# --- SIDE_EFFECT_CALLS ------------------------------------------------------


@RAISE
@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("tests.testapp.webhooks.post", lambda: webhooks.post("https://x")),
        ("tests.testapp.webhooks.Client.charge", lambda: webhooks.Client().charge(5)),
        ("tests.testapp.webhooks.Client.ping", lambda: webhooks.Client.ping()),
    ],
)
def test_custom_calls(connect, extra_calls, path, call):
    extra_calls(path)

    def receiver(sender, **kwargs):
        call()

    connect(post_save, receiver, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError) as caught, transaction.atomic():
        Invoice.objects.create()
    assert caught.value.call == f"{path}()"
    assert webhooks.CALLS == []

    # Still callable, and quiet, outside a transaction.
    call()
    assert len(webhooks.CALLS) == 1


@RAISE
def test_nested_side_effect_is_reported_once_for_the_outer_call(connect, extra_calls):
    extra_calls("tests.testapp.webhooks.post_and_email")

    def receiver(sender, **kwargs):
        webhooks.post_and_email("https://x")

    connect(post_save, receiver, sender=Invoice)
    with (
        override_settings(SIGNAL_GUARD={"SIDE_EFFECTS": "warn"}),
        pytest.warns(SideEffectBeforeCommitWarning) as record,
        transaction.atomic(),
    ):
        Invoice.objects.create()
    [warning] = [w for w in record if w.category is SideEffectBeforeCommitWarning]
    assert "tests.testapp.webhooks.post_and_email()" in str(warning.message)


def test_unknown_custom_call_is_rejected(extra_calls):
    with pytest.raises(ImproperlyConfigured, match=r"shop\.missing\.post"):
        extra_calls("shop.missing.post")
    with pytest.raises(ImproperlyConfigured, match=r"tests\.testapp\.webhooks\.nope"):
        extra_calls("tests.testapp.webhooks.nope")


# --- settings ---------------------------------------------------------------


def test_warn_mode_points_at_the_call(connect):
    connect(post_save, email_customer, sender=Invoice)
    with pytest.warns(SideEffectBeforeCommitWarning) as record, transaction.atomic():
        Invoice.objects.create()
    [warning] = [w for w in record if w.category is SideEffectBeforeCommitWarning]
    assert warning.filename.endswith(THIS_FILE)
    assert len(mail.outbox) == 1  # warn lets it through


@override_settings(SIGNAL_GUARD={"SIDE_EFFECTS": "log"})
def test_log_mode(connect, caplog):
    connect(post_save, email_customer, sender=Invoice)
    with caplog.at_level(logging.WARNING, logger="signal_guard"), transaction.atomic():
        Invoice.objects.create()
    [record] = caplog.records
    assert record.signal_guard_call == "Email to customer@example.com"
    assert record.signal_guard_receiver == "tests.test_side_effects.email_customer"


@override_settings(SIGNAL_GUARD={"SIDE_EFFECTS": "off"})
def test_off_mode(connect):
    connect(post_save, email_customer, sender=Invoice)
    with warnings.catch_warnings(), transaction.atomic():
        warnings.simplefilter("error", SideEffectBeforeCommitWarning)
        Invoice.objects.create()


@override_settings(
    SIGNAL_GUARD={
        "SIDE_EFFECTS": "raise",
        "IGNORE_RECEIVERS": ["tests.test_side_effects.email_*"],
    }
)
def test_ignore_receivers(connect):
    connect(post_save, email_customer, sender=Invoice)
    with transaction.atomic():
        Invoice.objects.create()
    assert len(mail.outbox) == 1


def test_third_party_receivers_need_check_library_calls(connect):
    connect(post_save, email_customer, sender=Invoice)
    settings = {"SIDE_EFFECTS": "raise", "FIRST_PARTY": ["shop"]}
    with override_settings(SIGNAL_GUARD=settings), transaction.atomic():
        Invoice.objects.create()

    settings["CHECK_LIBRARY_CALLS"] = True
    with (
        override_settings(SIGNAL_GUARD=settings),
        pytest.raises(SideEffectBeforeCommitError),
        transaction.atomic(),
    ):
        Invoice.objects.create()


# --- tasks queued while a row is being deleted -----------------------------


@RAISE
def test_task_queued_in_pre_delete_explains_on_commit_is_not_enough(
    connect, fake_celery
):
    # The field bug: the task re-reads the row, which is gone once the delete
    # commits, so it exits early and never cleans up.
    def queue_cleanup(sender, instance, **kwargs):
        fake_celery().delay(instance.pk)

    invoice = Invoice.objects.create()
    connect(pre_delete, queue_cleanup, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError) as caught:
        invoice.delete()
    message = str(caught.value)
    assert f"runs during pre_delete for testapp.Invoice #{invoice.pk}" in message
    assert "on_commit() alone will not fix it" in message
    assert "must not re-read" in message
    assert fake_celery.queued == []


@RAISE
def test_email_in_post_delete_keeps_the_usual_advice(connect):
    invoice = Invoice.objects.create()
    connect(post_delete, email_customer, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError) as caught:
        invoice.delete()
    assert "on_commit() alone" not in str(caught.value)


# --- allow_side_effect ------------------------------------------------------


@RAISE
def test_allow_side_effect_context_manager(connect):
    def cancel_at_vendor(sender, instance, **kwargs):
        with allow_side_effect(reason="the delete must roll back if this fails"):
            send_mail("cancel", "body", None, [instance.email])

    connect(post_save, cancel_at_vendor, sender=Invoice)
    with transaction.atomic():
        Invoice.objects.create()
    assert len(mail.outbox) == 1

    # Only inside the block.
    connect(post_save, email_customer, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError), transaction.atomic():
        Invoice.objects.create()


@RAISE
def test_allow_side_effect_decorates_a_receiver(connect):
    @allow_side_effect(reason="the vendor must accept before we commit")
    def cancel_at_vendor(sender, instance, **kwargs):
        send_mail("cancel", "body", None, [instance.email])

    invoice = Invoice.objects.create()
    connect(pre_delete, cancel_at_vendor, sender=Invoice)
    invoice.delete()
    assert len(mail.outbox) == 1


@RAISE
def test_allow_side_effect_async(connect):
    async def create(allowed):
        if not allowed:
            return await sync_to_async(Invoice.objects.create)()
        async with allow_side_effect(reason="async"):
            return await sync_to_async(Invoice.objects.create)()

    connect(post_save, email_customer, sender=Invoice)
    with transaction.atomic():
        async_to_sync(create)(True)
    assert len(mail.outbox) == 1
    with pytest.raises(SideEffectBeforeCommitError), transaction.atomic():
        async_to_sync(create)(False)


def test_allow_side_effect_needs_reason():
    with pytest.raises(TypeError):
        allow_side_effect()
    with pytest.raises(ValueError):
        allow_side_effect(reason=" ")


# --- blaming first-party code ----------------------------------------------


@override_settings(
    SIGNAL_GUARD={"SIDE_EFFECTS": "raise", "FIRST_PARTY": ["tests.test_side_effects"]}
)
def test_side_effect_in_a_library_blames_the_receiver_line(connect):
    def notify_vendor(sender, instance, **kwargs):
        sdk.notify(instance.email)  # blamed line

    connect(post_save, notify_vendor, sender=Invoice)
    with pytest.raises(SideEffectBeforeCommitError) as caught, transaction.atomic():
        Invoice.objects.create()
    message = str(caught.value)
    assert f"called at  {THIS_FILE}:" in message
    assert "fakevendor" not in message


@override_settings(
    SIGNAL_GUARD={"SIDE_EFFECTS": "warn", "FIRST_PARTY": ["tests.test_side_effects"]}
)
def test_side_effect_in_a_library_warns_at_the_receiver_line(connect):
    def notify_vendor(sender, instance, **kwargs):
        sdk.notify(instance.email)

    connect(post_save, notify_vendor, sender=Invoice)
    with pytest.warns(SideEffectBeforeCommitWarning) as record, transaction.atomic():
        Invoice.objects.create()
    [warning] = [w for w in record if w.category is SideEffectBeforeCommitWarning]
    assert warning.filename.endswith(THIS_FILE)
