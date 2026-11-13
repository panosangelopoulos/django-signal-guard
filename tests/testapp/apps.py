from django.apps import AppConfig


class TestAppConfig(AppConfig):
    name = "tests.testapp"
    label = "testapp"

    def ready(self) -> None:
        from . import signals  # noqa: F401
