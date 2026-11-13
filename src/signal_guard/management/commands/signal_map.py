from typing import ClassVar

from django.core.management.base import BaseCommand, CommandError

from signal_guard.signal_map import collect, diff, lock_path, lock_text, readable_text


class Command(BaseCommand):
    # The signal checks are a separate CI step: `manage.py check --tag signals`.
    requires_system_checks: ClassVar[list[str]] = []
    help = (
        "Show which receivers run for which signals and senders. With --write, "
        "save them to a lock file; with --check, fail when it is out of date."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--write", action="store_true", help="Write the lock file.")
        mode.add_argument(
            "--check",
            action="store_true",
            help="Exit with an error and a diff if the lock file is out of date.",
        )
        parser.add_argument(
            "--lock-file",
            help="Path of the lock file. Default: SIGNAL_GUARD['LOCK_FILE'].",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="include_all",
            help="Include Django's and third-party apps' internal receivers.",
        )

    def handle(self, *args, write, check, lock_file, include_all, **options):
        items = collect(include_all=include_all)

        if not (write or check):
            self.stdout.write(readable_text(items, include_all))
            return

        path = lock_path(lock_file)
        expected = lock_text(items)

        if write:
            path.write_text(expected, encoding="utf-8")
            self.stdout.write(f"Wrote {len(items)} receivers to {path}.")
            return

        if not path.exists():
            raise CommandError(
                f"{path} does not exist. Run `manage.py signal_map --write` and "
                "commit the file."
            )
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            self.stderr.write(diff(expected, actual, path))
            raise CommandError(
                f"{path.name} is out of date: receivers were added or removed. "
                "Review the diff above, then run `manage.py signal_map --write` "
                "and commit the file."
            )
        self.stdout.write(f"{path.name} is up to date ({len(items)} receivers).")
