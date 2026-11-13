"""Like easy_thumbnails and imagekit: receivers connected for every sender."""

from django.db.models.signals import post_save, pre_save


def find_uncommitted_filefields(sender, instance, **kwargs):
    pass


def post_save_receiver(sender, instance, **kwargs):
    pass


def connect():
    pre_save.connect(find_uncommitted_filefields, weak=False)
    post_save.connect(post_save_receiver, weak=False)


def disconnect():
    pre_save.disconnect(find_uncommitted_filefields)
    post_save.disconnect(post_save_receiver)
