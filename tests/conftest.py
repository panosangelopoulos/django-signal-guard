import sys

import pytest
from django.db.models.signals import post_save

from signal_guard import registry
from tests.testapp.models import Order


@pytest.fixture(autouse=True)
def isolate_registry():
    """Forget connect() records made by a test, so checks stay independent."""
    count = len(registry.RECORDS)
    yield
    del registry.RECORDS[count:]


@pytest.fixture
def connect():
    """Connect receivers for one test only: connect(signal, receiver, sender=...)."""
    connected = []

    def _connect(signal, receiver, sender=None, **kwargs):
        signal.connect(receiver, sender=sender, weak=False, **kwargs)
        connected.append((signal, receiver, sender))
        return receiver

    yield _connect
    for signal, receiver, sender in connected:
        signal.disconnect(receiver, sender=sender)


@pytest.fixture
def late_signals():
    """Import tests.testapp.late_signals fresh, after startup."""
    post_save.disconnect(sender=Order, dispatch_uid="late-receiver")
    sys.modules.pop("tests.testapp.late_signals", None)
    import tests.testapp.late_signals as module

    yield module
    post_save.disconnect(sender=Order, dispatch_uid="late-receiver")
    sys.modules.pop("tests.testapp.late_signals", None)
