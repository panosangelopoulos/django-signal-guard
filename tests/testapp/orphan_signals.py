"""Never imported: system check W001 must report it."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Order


@receiver(post_save, sender=Order)
def forgotten_receiver(sender, instance, **kwargs):
    pass
