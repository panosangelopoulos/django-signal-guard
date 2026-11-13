"""Stand-ins for a project's own side effects, listed in SIDE_EFFECT_CALLS."""

from django.core.mail import send_mail

CALLS: list[str] = []


def post(url):
    CALLS.append(url)


def post_and_email(url):
    send_mail("hook", url, None, ["ops@example.com"])
    CALLS.append(url)


class Client:
    def charge(self, amount):
        CALLS.append(f"charge {amount}")

    @staticmethod
    def ping():
        CALLS.append("ping")
