"""A vendor SDK that makes HTTP calls for you."""

from django.core.mail import send_mail


def notify(address):
    send_mail("vendor", "body", None, [address])
