import gc
import logging
import threading
import warnings

import pytest
from asgiref.sync import async_to_sync
from django.core.exceptions import ImproperlyConfigured
from django.db.models.query import QuerySet
from django.db.models.signals import post_save
from django.test import override_settings

from signal_guard import SignalBypassError, SignalBypassWarning, allow_bypass
from signal_guard.guard import install, is_allowed, is_installed, uninstall
from tests.testapp.models import Customer, Event, Note, Order, Tag

pytestmark = pytest.mark.django_db

THIS_FILE = "tests/test_guard.py"


# --- what gets caught -------------------------------------------------------


def test_bulk_create_raises_before_writing():
    with pytest.raises(SignalBypassError) as caught:
        Order.objects.bulk_create([Order(), Order()])

    assert Order.objects.count() == 0
    assert caught.value.model is Order
    assert caught.value.method == "bulk_create"
    assert [r.path for r in caught.value.receivers] == [
        "tests.testapp.signals.normalize_status",
        "tests.testapp.signals.reindex_order",
    ]


def test_message_names_receivers_and_call_site():
    with pytest.raises(SignalBypassError) as caught:
        Order.objects.bulk_create([Order()])  # line checked below

    message = str(caught.value)
    assert message.startswith("QuerySet.bulk_create() on testapp.Order skips 2")
    assert "pre_save   tests.testapp.signals.normalize_status" in message
    assert "post_save  tests.testapp.signals.reindex_order" in message
    assert "(tests/testapp/signals.py:" in message
    assert f"Called from {THIS_FILE}:" in message
    assert 'allow_bypass(Order, reason="...")' in message


def test_update_via_manager_and_queryset():
    Order.objects.create()
    with pytest.raises(SignalBypassError):
        Order.objects.update(status="paid")
    with pytest.raises(SignalBypassError):
        Order.objects.filter(status="new").update(status="paid")
    assert Order.objects.filter(status="paid").count() == 0


def test_bulk_update_is_reported_once():
    order = Order.objects.create()
    order.status = "paid"
    with (
        override_settings(SIGNAL_GUARD={"BYPASS": "warn"}),
        pytest.warns(SignalBypassWarning) as record,
    ):
        Order.objects.bulk_update([order], ["status"])
    assert len(record) == 1
    assert "QuerySet.bulk_update()" in str(record[0].message)


def test_custom_queryset_method_is_caught():
    Order.objects.create()
    with pytest.raises(SignalBypassError) as caught:
        Order.objects.mark_paid()
    assert "Called from tests/testapp/models.py:" in str(caught.value)


def test_receiver_for_all_senders_counts():
    def audit_everything(sender, **kwargs):
        pass

    post_save.connect(audit_everything)
    try:
        with pytest.raises(SignalBypassError) as caught:
            Tag.objects.bulk_create([Tag(name="a")])
        assert "audit_everything" in str(caught.value)
    finally:
        post_save.disconnect(audit_everything)


def test_async_bulk_create_is_caught():
    async def write():
        await Order.objects.abulk_create([Order()])

    with pytest.raises(SignalBypassError):
        async_to_sync(write)()


# --- what is left alone ----------------------------------------------------


def test_model_without_receivers_passes():
    Tag.objects.bulk_create([Tag(name="a")])
    Tag.objects.update(name="b")
    assert Tag.objects.get().name == "b"


def test_save_still_works():
    order = Order.objects.create(status="NEW")
    order.save()
    assert Order.objects.get().status == "new"


def test_bulk_safe_receiver_is_skipped():
    Note.objects.bulk_create([Note(text="a")])
    assert Note.objects.count() == 1


@override_settings(SIGNAL_GUARD={"BYPASS": "raise", "IGNORE_RECEIVERS": ["*.track_*"]})
def test_ignore_receivers_pattern():
    Event.objects.bulk_create([Event(kind="click")])
    assert Event.objects.count() == 1


def test_dead_weak_receiver_is_not_reported():
    def short_lived(sender, **kwargs):
        pass

    post_save.connect(short_lived, sender=Tag)
    del short_lived
    gc.collect()
    Tag.objects.bulk_create([Tag(name="a")])


def test_django_internal_writes_pass_by_default():
    customer = Customer.objects.create(name="c")
    order = Order.objects.create()
    customer.orders.add(order)  # reverse FK add() runs an UPDATE
    customer.delete()  # on_delete=SET_NULL runs an UPDATE
    assert Order.objects.get().customer is None


@override_settings(SIGNAL_GUARD={"BYPASS": "raise", "CHECK_LIBRARY_CALLS": True})
def test_django_internal_writes_can_be_checked():
    customer = Customer.objects.create(name="c")
    Order.objects.create(customer=customer)
    with pytest.raises(SignalBypassError):
        customer.delete()


@override_settings(SIGNAL_GUARD={"BYPASS": "raise", "FIRST_PARTY": ["tests.testapp"]})
def test_calls_from_installed_packages_pass_by_default():
    # With FIRST_PARTY set, this test module stands in for a library.
    Order.objects.bulk_create([Order()])
    with pytest.raises(SignalBypassError):
        Order.objects.mark_paid()  # defined in tests.testapp, first-party


@override_settings(
    SIGNAL_GUARD={
        "BYPASS": "raise",
        "FIRST_PARTY": ["tests.testapp"],
        "CHECK_LIBRARY_CALLS": True,
    }
)
def test_calls_from_installed_packages_can_be_checked():
    with pytest.raises(SignalBypassError):
        Order.objects.bulk_create([Order()])


# --- allow_bypass ----------------------------------------------------------


def test_allow_bypass_context_manager():
    with allow_bypass(Order, reason="reindexed in bulk afterwards"):
        Order.objects.bulk_create([Order(), Order()])
    assert Order.objects.count() == 2
    with pytest.raises(SignalBypassError):
        Order.objects.bulk_create([Order()])


def test_allow_bypass_is_per_model():
    with allow_bypass(Tag, reason="other model"), pytest.raises(SignalBypassError):
        Order.objects.bulk_create([Order()])


def test_allow_bypass_without_models_allows_all():
    with allow_bypass(reason="data migration"):
        Order.objects.bulk_create([Order()])
        Event.objects.bulk_create([Event(kind="x")])


def test_allow_bypass_as_decorator_and_nested():
    guard = allow_bypass(Order, reason="recursive import")

    @guard
    def import_rows(depth):
        Order.objects.bulk_create([Order()])
        if depth:
            import_rows(depth - 1)

    import_rows(2)
    assert Order.objects.count() == 3
    with pytest.raises(SignalBypassError):
        Order.objects.bulk_create([Order()])


def test_allow_bypass_instance_shared_across_threads():
    guard = allow_bypass(Order, reason="decorated view")
    both_inside = threading.Barrier(2, timeout=5)
    results, errors = [], []

    @guard
    def view():
        both_inside.wait()
        results.append(is_allowed(Order))
        both_inside.wait()

    def run():
        try:
            view()
            results.append(is_allowed(Order))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(results) == [False, False, True, True]
    assert not is_allowed(Order)


def test_allow_bypass_async():
    async def write():
        async with allow_bypass(Order, reason="async import"):
            await Order.objects.abulk_create([Order()])

    async_to_sync(write)()
    assert Order.objects.count() == 1


def test_allow_bypass_needs_reason():
    with pytest.raises(TypeError):
        allow_bypass(Order)
    with pytest.raises(ValueError):
        allow_bypass(Order, reason="  ")
    with pytest.raises(TypeError):
        allow_bypass("testapp.Order", reason="string is not a model")


# --- modes and settings ----------------------------------------------------


@override_settings(SIGNAL_GUARD={"BYPASS": "warn"})
def test_warn_mode_points_at_call_site():
    with pytest.warns(SignalBypassWarning) as record:
        Order.objects.bulk_create([Order()])
    assert record[0].filename == __file__
    assert Order.objects.count() == 1


@override_settings(SIGNAL_GUARD={"BYPASS": "log"})
def test_log_mode(caplog):
    with caplog.at_level(logging.WARNING, logger="signal_guard"):
        Order.objects.bulk_create([Order()])
    (record,) = caplog.records
    assert record.pathname == __file__
    assert record.signal_guard_model == "testapp.Order"
    assert record.signal_guard_method == "bulk_create"


@pytest.mark.parametrize(
    "config", [{"BYPASS": "off"}, {"ENABLED": False, "BYPASS": "raise"}]
)
def test_off_and_disabled(config):
    with override_settings(SIGNAL_GUARD=config), warnings.catch_warnings():
        warnings.simplefilter("error")
        Order.objects.bulk_create([Order()])


@pytest.mark.parametrize("config", [{"BYPASS": "loud"}, {"BYPAS": "warn"}, "warn"])
def test_bad_settings(config):
    with override_settings(SIGNAL_GUARD=config), pytest.raises(ImproperlyConfigured):
        Order.objects.bulk_create([Order()])


def test_install_is_idempotent_and_reversible():
    assert is_installed()
    wrapped = QuerySet.update
    install()
    assert QuerySet.update is wrapped
    uninstall()
    try:
        assert not is_installed()
        assert not hasattr(QuerySet.update, "__signal_guard_original__")
    finally:
        install()
    assert is_installed()
