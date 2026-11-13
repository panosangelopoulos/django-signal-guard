from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from signal_guard import bulk_safe

from .events import order_paid
from .models import Event, Note, Order


@receiver(pre_save, sender=Order)
def normalize_status(sender, instance, **kwargs):
    instance.status = instance.status.lower()


@receiver(post_save, sender=Order)
def reindex_order(sender, instance, created, **kwargs):
    pass


@receiver(post_save, sender=Note)
@bulk_safe
def warm_note_cache(sender, instance, **kwargs):
    pass


@receiver(post_save, sender=Event)
def track_event(sender, instance, **kwargs):
    pass


@receiver(order_paid, dispatch_uid="send-receipt")
def send_receipt(sender, **kwargs):
    pass
