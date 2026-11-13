"""Imported only inside a test, after startup: system check W002."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Order


@receiver(post_save, sender=Order, dispatch_uid="late-receiver")
def late_receiver(sender, instance, **kwargs):
    pass
