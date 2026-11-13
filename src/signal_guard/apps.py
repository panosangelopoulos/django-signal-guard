from django.apps import AppConfig

from . import registry
from .conf import get_config
from .guard import install

# Start recording connect() calls as soon as Django imports this app, before
# other apps import their models and run ready(). This is why signal_guard
# should come first in INSTALLED_APPS.
registry.install()


class SignalGuardConfig(AppConfig):
    name = "signal_guard"
    verbose_name = "Signal guard"

    def ready(self) -> None:
        from . import checks  # noqa: F401 - registers the system checks

        # Validate settings at startup so a typo fails fast.
        if get_config().enabled:
            install()
        else:
            registry.uninstall()
