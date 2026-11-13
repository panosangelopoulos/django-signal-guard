"""Reports that are noise: library receivers, migrations, update() overrides,
create-only receivers and a receiver's own update()."""

import logging

import pytest
from django.apps import apps
from django.db.migrations.state import ProjectState
from django.db.models.signals import post_save, pre_save
from django.test import override_settings

from signal_guard import SignalBypassError
from tests.fakevendor import thumbnails
from tests.testapp.models import Article, Invoice, Order, Tag

pytestmark = pytest.mark.django_db

THIS_FILE = "tests/test_noise.py"
# This file and the test app are first-party; tests.fakevendor is a library.
FIRST_PARTY = ["tests.testapp", "tests.test_noise"]


def guard(**settings):
    return override_settings(
        SIGNAL_GUARD={"BYPASS": "raise", "FIRST_PARTY": FIRST_PARTY, **settings}
    )


@pytest.fixture
def library_receivers():
    thumbnails.connect()
    yield
    thumbnails.disconnect()


# --- 1. library receivers connected for every sender -----------------------


@guard()
def test_global_library_receivers_are_ignored_by_default(library_receivers):
    Tag.objects.update(name="x")


@guard(LIBRARY_RECEIVERS="report")
def test_library_receivers_can_all_be_reported(library_receivers):
    with pytest.raises(SignalBypassError) as caught:
        Tag.objects.update(name="x")
    assert [r.path for r in caught.value.receivers] == [
        "tests.fakevendor.thumbnails.find_uncommitted_filefields",
        "tests.fakevendor.thumbnails.post_save_receiver",
    ]


@guard(LIBRARY_RECEIVERS="ignore_global")
def test_ignore_global_library_receivers(library_receivers, connect):
    Tag.objects.update(name="x")  # only the library's global receivers

    def library_for_tag(sender, **kwargs):
        pass

    # A library receiver connected to this model is still reported.
    library_for_tag.__module__ = "tests.fakevendor.history"
    connect(post_save, library_for_tag, sender=Tag)
    with pytest.raises(SignalBypassError) as caught:
        Tag.objects.update(name="y")
    assert [r.path for r in caught.value.receivers] == [
        "tests.fakevendor.history.test_ignore_global_library_receivers"
        ".<locals>.library_for_tag"
    ]


@guard(LIBRARY_RECEIVERS="ignore_global")
def test_ignore_global_keeps_first_party_global_receivers(library_receivers, connect):
    def audit_everything(sender, **kwargs):
        pass

    connect(post_save, audit_everything)
    with pytest.raises(SignalBypassError) as caught:
        Tag.objects.update(name="x")
    assert [r.path.rpartition(".")[2] for r in caught.value.receivers] == [
        "audit_everything"
    ]


@guard(LIBRARY_RECEIVERS="ignore")
def test_ignore_all_library_receivers(library_receivers, connect):
    def library_for_tag(sender, **kwargs):
        pass

    library_for_tag.__module__ = "tests.fakevendor.history"
    connect(post_save, library_for_tag, sender=Tag)
    Tag.objects.update(name="x")
    # First-party receivers are still reported.
    with pytest.raises(SignalBypassError):
        Order.objects.update(status="x")


@guard(LIBRARY_RECEIVERS="nope")
def test_library_receivers_setting_is_validated():
    with pytest.raises(Exception, match="LIBRARY_RECEIVERS"):
        Order.objects.update(status="x")


# --- 2. historical models in migrations ------------------------------------


def historical(model):
    state = ProjectState.from_apps(apps)
    return state.apps.get_model(model._meta.app_label, model._meta.model_name)


@guard()
def test_historical_models_are_ignored_by_default(connect):
    def audit_everything(sender, **kwargs):
        pass

    connect(post_save, audit_everything)
    HistoricalTag = historical(Tag)
    HistoricalTag.objects.bulk_create([HistoricalTag(name="a")])


@guard(IGNORE_HISTORICAL_MODELS=False)
def test_historical_models_can_be_reported(connect):
    def audit_everything(sender, **kwargs):
        pass

    connect(post_save, audit_everything)
    HistoricalTag = historical(Tag)
    assert HistoricalTag.__module__ == "__fake__"
    with pytest.raises(SignalBypassError):
        HistoricalTag.objects.bulk_create([HistoricalTag(name="a")])


@guard(IGNORE_HISTORICAL_MODELS=True)
def test_historical_models_can_be_ignored(connect):
    def audit_everything(sender, **kwargs):
        pass

    connect(post_save, audit_everything)
    HistoricalTag = historical(Tag)
    HistoricalTag.objects.bulk_create([HistoricalTag(name="a")])
    HistoricalTag.objects.update(name="b")
    # The real model is still checked.
    with pytest.raises(SignalBypassError):
        Tag.objects.update(name="c")


# --- 3. update() overrides -------------------------------------------------


def article_saved(sender, **kwargs):
    pass


@override_settings(SIGNAL_GUARD={"BYPASS": "raise"})
def test_call_site_walks_past_a_first_party_update_override(connect):
    # Without FIRST_PARTY, tests.fakevendor counts as first-party, like a
    # project's own TimestampedQuerySet.
    connect(post_save, article_saved, sender=Article)
    with pytest.raises(SignalBypassError) as caught:
        Article.objects.filter(pk=1).update(touched=False)  # the caller
    message = str(caught.value)
    assert f"Called from {THIS_FILE}:" in message
    assert "through tests/fakevendor/managers.py:" in message
    assert "TimestampedQuerySet.update()" in message


@guard()
def test_library_update_override_does_not_hide_the_caller(connect):
    connect(post_save, article_saved, sender=Article)
    with pytest.raises(SignalBypassError) as caught:
        Article.objects.update(touched=False)
    assert f"Called from {THIS_FILE}:" in str(caught.value)


@guard()
def test_own_queryset_methods_with_other_names_stay_the_call_site():
    with pytest.raises(SignalBypassError) as caught:
        Order.objects.mark_paid()
    assert "Called from tests/testapp/models.py:" in str(caught.value)
    assert "through" not in str(caught.value)


# --- 4. create-only receivers ----------------------------------------------


def welcome(sender, instance, created, **kwargs):
    """Docstrings are skipped."""
    if not created:
        return
    instance.welcomed = True


def welcome_nested(sender, instance, created, **kwargs):
    if created:
        instance.welcomed = True


def welcome_and(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        instance.welcomed = True


def welcome_or(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    instance.welcomed = True


def on_every_save(sender, instance, created, **kwargs):
    if created:
        instance.welcomed = True
    instance.saved = True


def if_else(sender, instance, created, **kwargs):
    if created:
        instance.welcomed = True
    else:
        instance.saved = True


def no_created_argument(sender, instance, **kwargs):
    if not kwargs["created"]:
        return


def early_exit_on_created(sender, instance, created, **kwargs):
    if created:
        return
    instance.saved = True


@pytest.mark.parametrize(
    ("receiver", "create_only"),
    [
        (welcome, True),
        (welcome_nested, True),
        (welcome_and, True),
        (welcome_or, True),
        (on_every_save, False),
        (if_else, False),
        (no_created_argument, False),
        (early_exit_on_created, False),
        (lambda sender, created, **kw: None, False),
        (print, False),
    ],
)
def test_create_only_heuristic(receiver, create_only):
    from signal_guard.heuristics import is_create_only

    assert is_create_only(receiver) is create_only


@guard()
def test_create_only_receivers_are_marked_by_default(connect):
    connect(post_save, welcome, sender=Invoice)
    with pytest.raises(SignalBypassError) as caught:
        Invoice.objects.update(email="x")
    message = str(caught.value)
    assert "tests.test_noise.welcome" in message
    assert "[create-only?]" in message
    assert "IGNORE_CREATE_ONLY_RECEIVERS" in message
    assert caught.value.receivers[0].create_only


@guard(IGNORE_CREATE_ONLY_RECEIVERS=True)
def test_create_only_receivers_can_be_ignored_for_updates(connect):
    connect(post_save, welcome, sender=Invoice)
    invoice = Invoice.objects.create()
    Invoice.objects.update(email="x")
    invoice.email = "y"
    Invoice.objects.bulk_update([invoice], ["email"])
    # bulk_create() does create rows, so the receiver is skipped for real.
    with pytest.raises(SignalBypassError):
        Invoice.objects.bulk_create([Invoice()])


@guard(IGNORE_CREATE_ONLY_RECEIVERS=True)
def test_other_receivers_are_still_reported(connect):
    connect(post_save, welcome, sender=Invoice)
    connect(post_save, on_every_save, sender=Invoice)
    with pytest.raises(SignalBypassError) as caught:
        Invoice.objects.update(email="x")
    assert [r.path for r in caught.value.receivers] == [
        "tests.test_noise.on_every_save"
    ]


@guard()
def test_create_only_is_not_marked_for_bulk_create(connect):
    connect(post_save, welcome, sender=Invoice)
    with pytest.raises(SignalBypassError) as caught:
        Invoice.objects.bulk_create([Invoice()])
    assert "create-only" not in str(caught.value)


# --- 5. a receiver's own update() -------------------------------------------


def stamp_invoice(sender, instance, **kwargs):
    # update() on purpose, so this receiver does not run again.
    Invoice.objects.filter(pk=instance.pk).update(email="stamped@example.com")


@guard()
def test_self_update_is_reported_with_a_note(connect):
    connect(post_save, stamp_invoice, sender=Invoice)
    with pytest.raises(SignalBypassError) as caught:
        Invoice.objects.create()
    message = str(caught.value)
    assert "runs inside tests.test_noise.stamp_invoice" in message
    assert "a post_save receiver for testapp.Invoice" in message
    assert "IGNORE_RECEIVER_SELF_UPDATES" in message


@guard(IGNORE_RECEIVER_SELF_UPDATES=True)
def test_self_update_can_be_ignored(connect):
    connect(post_save, stamp_invoice, sender=Invoice)
    Invoice.objects.create()
    assert Invoice.objects.get().email == "stamped@example.com"


@guard(IGNORE_RECEIVER_SELF_UPDATES=True)
def test_update_of_another_model_in_a_receiver_is_reported(connect):
    def touch_orders(sender, instance, **kwargs):
        Order.objects.update(status="x")

    connect(post_save, touch_orders, sender=Invoice)
    with pytest.raises(SignalBypassError) as caught:
        Invoice.objects.create()
    assert "runs inside" not in str(caught.value)


@guard(IGNORE_RECEIVER_SELF_UPDATES=True)
def test_self_update_from_pre_save_counts_too(connect):
    def stamp_before(sender, instance, **kwargs):
        if instance.pk:
            Invoice.objects.filter(pk=instance.pk).update(email="early")

    invoice = Invoice.objects.create()
    connect(pre_save, stamp_before, sender=Invoice)
    invoice.save()


# --- logging fields ---------------------------------------------------------


@guard(BYPASS="log")
def test_log_records_carry_call_site_and_test(caplog):
    with caplog.at_level(logging.WARNING, logger="signal_guard"):
        Order.objects.update(status="x")
    [record] = caplog.records
    assert record.signal_guard_kind == "bypass"
    assert record.signal_guard_location.startswith(f"{THIS_FILE}:")
    assert record.signal_guard_test == (
        f"{THIS_FILE}::test_log_records_carry_call_site_and_test"
    )
