"""signal_guard.testing: assertions about reports and about signals sent."""

import logging
import warnings

import pytest
from asgiref.sync import async_to_sync
from django.core.mail import send_mail
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import Signal
from django.test import TestCase, override_settings

from signal_guard import SignalBypassError, allow_bypass, tracing
from signal_guard.testing import (
    Reports,
    SignalGuardAssertionsMixin,
    assert_bypass,
    assert_connected,
    assert_no_reports,
    assert_not_connected,
    assert_not_sent,
    assert_recursion,
    assert_sent,
    assert_side_effect,
    capture_reports,
    capture_signals,
)
from tests.testapp.models import Counter, Invoice, Order, Tag

pytestmark = pytest.mark.django_db

THIS_FILE = "tests/test_testing.py"
order_paid = Signal()


def email_customer(sender, instance, **kwargs):
    send_mail("Thanks", "Your invoice", None, [instance.email])


def save_again(sender, instance, **kwargs):
    instance.save()


# --- capture_reports --------------------------------------------------------


def test_capture_reports_collects_instead_of_raising():
    with capture_reports() as reports:
        Order.objects.bulk_create([Order()])  # BYPASS is "raise" in tests
        Order.objects.update(status="x")
    assert isinstance(reports, Reports)
    assert [r.kind for r in reports] == ["bypass", "bypass"]
    first = reports[0]
    assert first.location.startswith(f"{THIS_FILE}:")
    assert (
        first.where == f"{THIS_FILE}::test_capture_reports_collects_instead_of_raising"
    )
    assert first.error.model is Order
    assert first.error.method == "bulk_create"
    assert first.message.startswith("QuerySet.bulk_create() on testapp.Order")
    assert (
        first.test == f"{THIS_FILE}::test_capture_reports_collects_instead_of_raising"
    )
    # Raised again once the block is over.
    with pytest.raises(SignalBypassError):
        Order.objects.update(status="y")


@pytest.mark.parametrize("mode", ["warn", "log"])
def test_capture_reports_silences_every_mode(mode, caplog):
    with (
        override_settings(SIGNAL_GUARD={"BYPASS": mode}),
        warnings.catch_warnings(),
        caplog.at_level(logging.WARNING, logger="signal_guard"),
    ):
        warnings.simplefilter("error")
        with capture_reports() as reports:
            Order.objects.update(status="x")
    assert len(reports) == 1
    assert caplog.records == []


@override_settings(SIGNAL_GUARD={"BYPASS": "off"})
def test_capture_reports_respects_off():
    with capture_reports() as reports:
        Order.objects.update(status="x")
    assert reports == []


def test_reports_helpers(connect):
    connect(post_save, email_customer, sender=Invoice)
    with capture_reports() as reports, transaction.atomic():
        Order.objects.update(status="x")
        Invoice.objects.create()
    assert [r.kind for r in reports.bypasses] == ["bypass"]
    assert [r.kind for r in reports.side_effects] == ["side_effect"]
    assert reports.recursions == []
    with pytest.raises(AssertionError, match="2 signal-guard reports"):
        reports.assert_none()
    Reports().assert_none()


def test_fingerprints_are_stable(connect):
    connect(post_save, email_customer, sender=Invoice)
    with capture_reports() as reports, transaction.atomic():
        Order.objects.update(status="x")
        Invoice.objects.create(email="a@example.com")
        Invoice.objects.create(email="b@example.com")
    bypass, email_a, email_b = reports
    assert bypass.fingerprint == (
        "testapp.Order.update() skips tests.testapp.signals.normalize_status, "
        "tests.testapp.signals.reindex_order"
    )
    # No addresses, URLs or primary keys: the same problem has one fingerprint.
    assert email_a.fingerprint == email_b.fingerprint
    assert email_a.fingerprint == "tests.test_testing.email_customer: Email"


def test_capture_reports_in_async_code():
    async def write():
        await Order.objects.abulk_create([Order()])

    with capture_reports() as reports:
        async_to_sync(write)()
    assert len(reports) == 1


# --- assert_no_reports ------------------------------------------------------


def test_assert_no_reports_passes_when_quiet():
    with assert_no_reports():
        Tag.objects.update(name="x")


def test_assert_no_reports_fails_with_every_message():
    with pytest.raises(AssertionError) as caught, assert_no_reports():
        Order.objects.update(status="x")
        Order.objects.bulk_create([Order()])
    message = str(caught.value)
    assert message.startswith("2 signal-guard reports:")
    assert "QuerySet.update() on testapp.Order" in message
    assert "QuerySet.bulk_create() on testapp.Order" in message


def test_assert_no_reports_by_kind(connect):
    connect(post_save, email_customer, sender=Invoice)
    # Only side effects are asserted on; the bypass is raised as usual.
    with (
        pytest.raises(SignalBypassError),
        assert_no_reports("side_effect"),
    ):
        Order.objects.update(status="x")
    with (
        pytest.raises(AssertionError, match="Email"),
        assert_no_reports("side_effect"),
        transaction.atomic(),
    ):
        Invoice.objects.create()


def test_assert_no_reports_as_decorator():
    @assert_no_reports()
    def quiet():
        Tag.objects.update(name="x")

    @assert_no_reports()
    def noisy():
        Order.objects.update(status="x")

    quiet()
    with pytest.raises(AssertionError):
        noisy()


def test_assert_no_reports_rejects_unknown_kinds():
    with pytest.raises(ValueError, match="bypas"):
        assert_no_reports("bypas")


def test_allowed_writes_are_not_reports():
    with assert_no_reports(), allow_bypass(Order, reason="tested"):
        Order.objects.update(status="x")


# --- assert_bypass ----------------------------------------------------------


def test_assert_bypass_matches_model_method_and_receivers():
    with assert_bypass(
        Order, method="update", receivers=["*.reindex_order"]
    ) as reports:
        Order.objects.update(status="x")
    assert len(reports) == 1
    with assert_bypass("testapp.Order"):
        Order.objects.bulk_create([Order()])


def test_assert_bypass_fails_when_nothing_is_reported():
    with (
        pytest.raises(AssertionError, match=r"Expected a bypass of testapp\.Tag"),
        assert_bypass(Tag),
    ):
        Tag.objects.update(name="x")


def test_assert_bypass_lets_other_reports_through():
    # A bypass of another model is not swallowed by the assertion.
    with pytest.raises(SignalBypassError), assert_bypass(Tag):
        Order.objects.update(status="x")


def test_assert_bypass_count():
    with assert_bypass(Order, count=2):
        Order.objects.update(status="x")
        Order.objects.update(status="y")
    with (
        pytest.raises(AssertionError, match=r"Expected 1 bypass.*got 2"),
        assert_bypass(Order, count=1),
    ):
        Order.objects.update(status="x")
        Order.objects.update(status="y")


def test_assert_bypass_wrong_receivers():
    # The bypass that did happen is not the one expected: it is emitted as
    # usual, and in raise mode that is the error the test sees.
    with pytest.raises(SignalBypassError), assert_bypass(Order, receivers=["*.nope"]):
        Order.objects.update(status="x")


@override_settings(SIGNAL_GUARD={"BYPASS": "log"})
def test_assert_bypass_lists_what_was_reported_instead():
    with (
        pytest.raises(AssertionError) as caught,
        assert_bypass(Order, receivers=["*.nope"]),
    ):
        Order.objects.update(status="x")
    message = str(caught.value)
    assert message.startswith("Expected a bypass of testapp.Order skipping *.nope")
    assert "Reported instead:" in message
    assert "QuerySet.update() on testapp.Order" in message


@override_settings(SIGNAL_GUARD={"BYPASS": "off"})
def test_assert_bypass_explains_off():
    with (
        pytest.raises(AssertionError, match=r"SIGNAL_GUARD\['BYPASS'\] is 'off'"),
        assert_bypass(Order),
    ):
        Order.objects.update(status="x")


# --- assert_side_effect and assert_recursion -------------------------------


def test_assert_side_effect(connect):
    connect(post_save, email_customer, sender=Invoice)
    with (
        assert_side_effect("Email*", receiver="*.email_customer") as reports,
        transaction.atomic(),
    ):
        Invoice.objects.create()
    assert reports[0].error.call == "Email to customer@example.com"


def test_assert_side_effect_fails(connect):
    with (
        pytest.raises(AssertionError, match="Expected a side effect"),
        assert_side_effect("Email*"),
        transaction.atomic(),
    ):
        Invoice.objects.create()


def test_assert_recursion(connect):
    connect(post_save, save_again, sender=Counter)
    with assert_recursion(receiver="*.save_again") as reports:
        Counter.objects.create()
    assert len(reports) == 1
    assert reports[0].fingerprint == (
        "post_save testapp.Counter: tests.test_testing.save_again"
        " → tests.test_testing.save_again"
    )


# --- signals sent -----------------------------------------------------------


def test_capture_signals():
    with capture_signals(post_save, pre_save) as sent:
        order = Order.objects.create()
    assert [s.signal_name for s in sent] == ["pre_save", "post_save"]
    assert sent[1].sender is Order
    assert sent[1].instance is order
    assert sent[1].kwargs["created"] is True
    assert [path for path, _ in sent[1].responses] == [
        "tests.testapp.signals.reindex_order"
    ]


def test_capture_signals_without_receivers_and_with_sender_filter():
    with capture_signals(sender=Tag) as sent:
        Tag.objects.create(name="a")  # Tag has no receivers at all
        Order.objects.create()
    # Model.__init__ always sends pre_init and post_init.
    assert [s.signal_name for s in sent] == [
        "pre_init",
        "post_init",
        "pre_save",
        "post_save",
    ]
    assert all(s.sender is Tag for s in sent)


def test_capture_custom_signal():
    with capture_signals(order_paid) as sent:
        order_paid.send(sender=Order, amount=5)
    assert sent[0].kwargs == {"amount": 5}
    assert sent[0].signal_name == "order_paid"


def test_assert_sent():
    with assert_sent(post_save, sender=Order, created=True) as sent:
        Order.objects.create()
    assert len(sent) == 1
    order = Order.objects.get()
    with (
        pytest.raises(AssertionError, match=r"post_save.*testapp\.Order.*created=True"),
        assert_sent(post_save, sender=Order, created=True),
    ):
        order.save()
    with assert_sent(post_save, instance=order, count=1):
        order.save()


def test_assert_not_sent(connect):
    # Without a receiver, Django fast-deletes and never sends post_delete.
    connect(post_delete, lambda sender, **kwargs: None, sender=Order)
    order = Order.objects.create()
    with assert_not_sent(post_delete, sender=Order):
        order.save()
    with (
        pytest.raises(AssertionError, match="post_delete was sent 1 time"),
        assert_not_sent(post_delete, sender=Order),
    ):
        order.delete()


def test_capture_signals_installs_tracing_when_disabled():
    tracing.uninstall()
    try:
        with capture_signals(post_save) as sent:
            Order.objects.create()
        assert len(sent) == 1
        assert not tracing.is_installed()
    finally:
        tracing.install()


# --- receivers connected ----------------------------------------------------


def test_assert_connected():
    from tests.testapp.signals import reindex_order

    assert_connected(post_save, reindex_order, sender=Order)
    assert_connected(post_save, "tests.testapp.signals.reindex_order", sender=Order)
    assert_connected(post_save, "*.reindex_*", sender=Order)
    assert_not_connected(post_save, reindex_order, sender=Tag)
    with pytest.raises(AssertionError, match=r"reindex_order is not connected"):
        assert_connected(post_save, reindex_order, sender=Tag)
    with pytest.raises(AssertionError, match="is connected"):
        assert_not_connected(post_save, "*.reindex_order", sender=Order)


def test_assert_connected_counts_receivers_for_every_sender(connect):
    def audit(sender, **kwargs):
        pass

    connect(post_save, audit)
    assert_connected(post_save, audit, sender=Tag)


# --- unittest -------------------------------------------------------------


class MixinTests(SignalGuardAssertionsMixin, TestCase):
    def test_methods(self):
        with self.assertBypass(Order, method="update"):
            Order.objects.update(status="x")
        with self.assertNoSignalReports():
            Tag.objects.update(name="x")
        with self.assertSignalSent(post_save, sender=Tag):
            Tag.objects.create(name="a")
        with self.assertSignalNotSent(post_save, sender=Order):
            Tag.objects.create(name="b")
        self.assertReceiverConnected(post_save, "*.reindex_order", sender=Order)
        self.assertReceiverNotConnected(post_save, "*.reindex_order", sender=Tag)
        with self.assertRaises(AssertionError), self.assertNoSignalReports():
            Order.objects.update(status="x")


# --- pytest fixtures --------------------------------------------------------


def test_signal_reports_fixture(signal_reports):
    Order.objects.update(status="x")
    assert len(signal_reports.bypasses) == 1


def test_sent_signals_fixture(sent_signals):
    Tag.objects.create(name="a")
    assert [s.signal_name for s in sent_signals][-2:] == ["pre_save", "post_save"]
