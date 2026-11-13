"""A queryset that overrides update(), as timestamping libraries do."""

from django.db import models


class TimestampedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        kwargs.setdefault("touched", True)
        return super().update(**kwargs)
