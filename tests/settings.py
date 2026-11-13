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

# Django 6.1+ deprecates the implicit default mailer; older versions ignore this.
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
