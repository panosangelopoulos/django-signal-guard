import gc

from django.apps import apps
from django.core.checks import run_checks
from django.db.models.signals import post_save
from django.test import override_settings

from signal_guard import registry
from signal_guard.checks import check_signals
from tests.testapp.models import Order, Tag

THIS_FILE = "tests/test_checks.py"


def messages(check_id):
    return [m for m in run_checks(tags=["signals"]) if m.id == check_id]


def dup_handler(sender, **kwargs):
    pass


# --- registry ----------------------------------------------------------------


def test_registry_records_startup_connects():
    (record,) = [
        r
        for r in registry.RECORDS
        if r.receiver_path == "tests.testapp.signals.reindex_order"
    ]
    assert record.late is False
    assert record.sender_label == "testapp.Order"
    assert record.connected_at.startswith("tests/testapp/signals.py:")


# --- W001 --------------------------------------------------------------------


def test_unimported_receiver_module_is_reported():
    (message,) = [
        m for m in messages("signal_guard.W001") if m.obj.endswith("orphan_signals")
    ]
    assert message.msg == (
        "tests/testapp/orphan_signals.py defines receivers that never run: "
        "forgotten_receiver."
    )
    assert "Nothing imports tests.testapp.orphan_signals." in message.hint
    assert "tests.testapp.apps.TestAppConfig.ready()" in message.hint


def test_connected_receiver_modules_are_not_reported():
    reported = {m.obj for m in messages("signal_guard.W001")}
    assert "tests.testapp.signals" not in reported


def test_checks_can_be_limited_to_apps():
    only_auth = check_signals(app_configs=[apps.get_app_config("auth")])
    assert [m for m in only_auth if m.id == "signal_guard.W001"] == []


# --- W002 --------------------------------------------------------------------


def test_late_connection_is_reported(late_signals):
    (message,) = messages("signal_guard.W002")
    assert message.msg.startswith(
        "tests.testapp.late_signals.late_receiver is connected only after "
        f"startup, when {THIS_FILE}:"
    ) or message.msg.startswith(
        "tests.testapp.late_signals.late_receiver is connected only after "
        "startup, when tests/conftest.py:"
    )
    assert "Celery workers" in message.hint
    # W001 does not report the same receiver a second time.
    assert not [
        m for m in messages("signal_guard.W001") if m.obj.endswith("late_signals")
    ]


# --- W003 --------------------------------------------------------------------


def test_dead_weak_receiver_is_reported():
    def short_lived(sender, **kwargs):
        pass

    post_save.connect(short_lived, sender=Tag)
    del short_lived
    gc.collect()

    (message,) = messages("signal_guard.W003")
    assert "test_dead_weak_receiver_is_reported.<locals>.short_lived" in message.msg
    assert f"(connected at {THIS_FILE}:" in message.msg
    assert "weak=False" in message.hint


def test_strong_receiver_is_not_dead():
    post_save.connect(
        lambda sender, **kw: None, sender=Tag, weak=False, dispatch_uid="x"
    )
    try:
        gc.collect()
        assert messages("signal_guard.W003") == []
    finally:
        post_save.disconnect(sender=Tag, dispatch_uid="x")


def test_disconnected_receiver_is_not_dead():
    def temporary(sender, **kwargs):
        pass

    post_save.connect(temporary, sender=Tag)
    post_save.disconnect(temporary, sender=Tag)
    del temporary
    gc.collect()
    assert messages("signal_guard.W003") == []


# --- W004 --------------------------------------------------------------------


def test_same_function_under_two_dispatch_uids():
    post_save.connect(dup_handler, sender=Tag, dispatch_uid="first")
    post_save.connect(dup_handler, sender=Tag, dispatch_uid="second")
    try:
        (message,) = messages("signal_guard.W004")
        assert message.msg == (
            "tests.test_checks.dup_handler is connected 2 times to "
            "django.db.models.signals.post_save for sender testapp.Tag "
            "(dispatch_uid first, second), so it runs 2 times per signal."
        )
    finally:
        post_save.disconnect(sender=Tag, dispatch_uid="first")
        post_save.disconnect(sender=Tag, dispatch_uid="second")


def test_closure_created_twice():
    def make_handler():
        def handler(sender, **kwargs):
            pass

        return handler

    first, second = make_handler(), make_handler()
    post_save.connect(first, sender=Tag, weak=False)
    post_save.connect(second, sender=Tag, weak=False)
    try:
        (message,) = messages("signal_guard.W004")
        assert "make_handler.<locals>.handler is connected 2 times" in message.msg
    finally:
        post_save.disconnect(first, sender=Tag)
        post_save.disconnect(second, sender=Tag)


def test_bound_methods_of_different_objects_are_not_duplicates():
    class Index:
        def on_save(self, sender, **kwargs):
            pass

    one, two = Index(), Index()
    post_save.connect(one.on_save, sender=Order, weak=False)
    post_save.connect(two.on_save, sender=Order, weak=False)
    try:
        assert messages("signal_guard.W004") == []
    finally:
        post_save.disconnect(one.on_save, sender=Order)
        post_save.disconnect(two.on_save, sender=Order)


# --- settings ----------------------------------------------------------------


@override_settings(SIGNAL_GUARD={"ENABLED": False})
def test_disabled():
    assert check_signals() == []
