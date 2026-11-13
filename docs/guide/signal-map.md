# Signal map and `signals.lock`

See which receivers run for which signal and sender: yours, plus third-party
receivers that run on your models. Add `--all` for Django's internals.

```console
$ python manage.py signal_map
First-party receivers (3)
  django.db.models.signals.post_save
    shop.Order
      shop.signals.reindex_order  (shop/signals.py:9)
      shop.webhooks.send_webhook  (shop/webhooks.py:6)
  django.db.models.signals.pre_save
    shop.Order
      shop.signals.normalize_status  (shop/signals.py:5)
```

Receivers connected with
[`@receiver_on_commit`](side-effects.md#the-fix-receiver_on_commit) are
labelled `on commit`.

## The lock file

Commit the same list as a lock file:

```bash
python manage.py signal_map --write   # writes signals.lock
python manage.py signal_map --check   # fails with a diff when it's out of date
```

```text
[first-party]
django.db.models.signals.post_save | shop.Order | shop.signals.reindex_order
django.db.models.signals.post_save | shop.Order | shop.webhooks.send_webhook
django.db.models.signals.pre_save | shop.Order | shop.signals.normalize_status
```

One line per receiver, with no line numbers, so the file only changes when a
receiver is added, removed or moved. That includes receivers a **dependency
upgrade** quietly adds to your models. With `--check` in CI, every such
change shows up in code review.

Set where the file lives with
[`LOCK_FILE`](../reference/configuration.md#lock_file). A relative path is
resolved against `settings.BASE_DIR` when it is set.

## Which receivers are listed

By default: your own receivers, plus receivers from installed packages that
run on your models, either connected to one of them or connected to every
sender of a model signal. What counts as yours follows
[`FIRST_PARTY`](../reference/configuration.md#first_party). `--all` lists
everything, including Django's own receivers.
