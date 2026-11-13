import logging
import warnings

import pytest
from asgiref.sync import async_to_sync
from django.core.exceptions import ImproperlyConfigured
from django.db.models.signals import post_save
from django.dispatch import Signal
from django.test import override_settings

from signal_guard import SignalRecursionError, SignalRecursionWarning
from signal_guard.conf import get_config
from signal_guard.tracing import is_dispatching, is_installed
from tests.testapp.models import Counter, Tag

pytestmark = pytest.mark.django_db

THIS_FILE = "tests/test_tracing.py"
RAISE = override_settings(SIGNAL_GUARD={"BYPASS": "raise", "RECURSION": "raise"})


def save_forever(sender, instance, **kwargs):
    instance.value += 1
    instance.save()


def save_until_four(sender, instance, **kwargs):
    if instance.value < 4:
        instance.value += 1
        instance.save()


def test_tracing_is_installed():
    assert is_installed()
    assert not is_dispatching()


@RAISE
def test_infinite_loop_is_stopped_on_the_third_entry(connect):
    connect(post_save, save_forever, sender=Counter)

    with pytest.raises(SignalRecursionError) as caught:
        Counter.objects.create()

    # Entered twice, stopped before the third run.
    assert len(caught.value.chain) == 3
    hop = caught.value.chain[0]
    assert hop.signal_name == "post_save"
    assert hop.target.startswith("testapp.Counter #")
    assert hop.receiver == "tests.test_tracing.save_forever"
    assert hop.call == "Counter.save()"
    assert hop.sent_at.startswith(f"{THIS_FILE}:")


@RAISE
def test_message_shows_the_chain(connect):
    connect(post_save, save_forever, sender=Counter)
    with pytest.raises(SignalRecursionError) as caught:
        Counter.objects.create()

    lines = str(caught.value).splitlines()
    assert lines[0].startswith("tests.test_tracing.save_forever re-entered for ")
    assert lines[1].startswith("  post_save  testapp.Counter #")
    assert "→ tests.test_tracing.save_forever  (tests/test_tracing.py:" in lines[1]
    assert lines[2].startswith(f"    Counter.save()  {THIS_FILE}:")
    assert lines[5].endswith("← this send")
    assert "QuerySet.update()" in lines[-1]


@RAISE
def test_saving_once_more_is_allowed(connect):
    def save_again_once(sender, instance, created, **kwargs):
        if created:
            instance.save()

    connect(post_save, save_again_once, sender=Counter)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Counter.objects.create()


@RAISE
def test_different_objects_are_not_a_loop(connect):
    def create_next(sender, instance, **kwargs):
        if Counter.objects.count() < 5:
            Counter.objects.create()

    connect(post_save, create_next, sender=Counter)
    Counter.objects.create()
    assert Counter.objects.count() == 5


@RAISE
def test_a_fresh_copy_of_the_same_row_is_a_loop(connect):
    def save_a_copy(sender, instance, **kwargs):
        Counter.objects.get(pk=instance.pk).save()

    connect(post_save, save_a_copy, sender=Counter)
    with pytest.raises(SignalRecursionError):
        Counter.objects.create()


@RAISE
def test_cycle_through_two_models(connect):
    def counter_saves_tag(sender, instance, **kwargs):
        Tag.objects.get_or_create(pk=instance.pk, defaults={"name": "x"})[0].save()

    def tag_saves_counter(sender, instance, **kwargs):
        Counter.objects.get(pk=instance.pk).save()

    connect(post_save, counter_saves_tag, sender=Counter)
    connect(post_save, tag_saves_counter, sender=Tag)

    with pytest.raises(SignalRecursionError) as caught:
        Counter.objects.create()

    receivers = [hop.receiver for hop in caught.value.chain[:-1]]
    assert receivers[:2] == [
        "tests.test_tracing.test_cycle_through_two_models.<locals>.counter_saves_tag",
        "tests.test_tracing.test_cycle_through_two_models.<locals>.tag_saves_counter",
    ]


@RAISE
def test_custom_signal_without_instance(connect):
    ping = Signal()

    def ping_again(sender, **kwargs):
        ping.send(sender=None)

    connect(ping, ping_again)
    with pytest.raises(SignalRecursionError) as caught:
        ping.send(sender=None)
    assert caught.value.chain[0].target == "any sender"
    assert caught.value.chain[0].call == "Signal.send()"


def test_warn_mode_warns_once_at_the_call_site(connect):
    connect(post_save, save_until_four, sender=Counter)
    with pytest.warns(SignalRecursionWarning) as record:
        counter = Counter.objects.create()

    assert len(record) == 1
    assert record[0].filename.endswith(THIS_FILE)
    counter.refresh_from_db()
    assert counter.value == 4


@override_settings(SIGNAL_GUARD={"RECURSION": "log"})
def test_log_mode(connect, caplog):
    connect(post_save, save_until_four, sender=Counter)
    with caplog.at_level(logging.WARNING, logger="signal_guard"):
        Counter.objects.create()
    [record] = caplog.records
    assert "re-entered for testapp.Counter" in record.getMessage()
    assert record.pathname.endswith(THIS_FILE)


@override_settings(SIGNAL_GUARD={"RECURSION": "off"})
def test_off_mode(connect):
    connect(post_save, save_until_four, sender=Counter)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Counter.objects.create()


def test_send_robust_and_asend_are_traced(connect):
    ping = Signal()
    seen = []

    def record_dispatch(sender, **kwargs):
        seen.append(is_dispatching())

    connect(ping, record_dispatch)
    ping.send_robust(sender=None)
    async_to_sync(ping.asend)(sender=None)
    async_to_sync(ping.asend_robust)(sender=None)
    assert seen == [True, True, True]
    assert not is_dispatching()


def test_invalid_mode_is_rejected():
    with (
        override_settings(SIGNAL_GUARD={"RECURSION": "loud"}),
        pytest.raises(ImproperlyConfigured, match="RECURSION"),
    ):
        get_config()
