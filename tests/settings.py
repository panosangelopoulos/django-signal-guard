SECRET_KEY = "signal-guard-tests"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

INSTALLED_APPS = [
    "signal_guard",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "tests.testapp",
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

SIGNAL_GUARD = {"BYPASS": "raise"}
