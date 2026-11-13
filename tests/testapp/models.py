from django.db import models


class OrderQuerySet(models.QuerySet):
    def mark_paid(self):
        # A project's own queryset method: the guard must see through it.
        return self.update(status="paid")


class Customer(models.Model):
    name = models.CharField(max_length=50)


class Order(models.Model):
    customer = models.ForeignKey(
        Customer, null=True, on_delete=models.SET_NULL, related_name="orders"
    )
    status = models.CharField(max_length=20, default="new")

    objects = OrderQuerySet.as_manager()


class Tag(models.Model):
    """No receivers at all."""

    name = models.CharField(max_length=50)


class Note(models.Model):
    """Only a receiver marked @bulk_safe."""

    text = models.CharField(max_length=50)


class Event(models.Model):
    """Only a receiver that tests ignore with IGNORE_RECEIVERS."""

    kind = models.CharField(max_length=50)
