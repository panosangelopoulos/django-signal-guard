"""System checks for receivers that never run, or run more than once.

Run them with ``python manage.py check --tag signals``. All four are warnings,
so ``check --fail-level WARNING`` makes them fail CI. Silence one with
``SILENCED_SYSTEM_CHECKS = ["signal_guard.W001"]``.

W001  A module defines @receiver functions but is never imported.
W002  A receiver is connected only after startup, by a lazy import.
W003  A weakly connected receiver was garbage-collected.
W004  The same receiver is connected more than once to one signal and sender.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from typing import Any

from django.apps import AppConfig, apps
from django.core import checks

from . import registry
from .conf import get_config
from .inspection import is_first_party, iter_live_receivers, load_urlconf
from .receivers import relative_path

_SKIP_DIRS = frozenset(
    {
        "migrations",
        "tests",
        "test",
        "__pycache__",
        "node_modules",
        "static",
        "templates",
        ".git",
        ".venv",
        "venv",
    }
)


@checks.register("signals")
def check_signals(
    app_configs: Iterable[AppConfig] | None = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    if not get_config().enabled:
        return []
    load_urlconf()
    messages = [
        *check_unimported_receiver_modules(app_configs),
        *check_late_receivers(),
        *check_dead_receivers(),
        *check_duplicate_receivers(),
    ]
    if app_configs is not None:
        labels = {config.label for config in app_configs}
        messages = [m for m in messages if _app_label(m.obj) in labels]
    return messages


def _app_label(module_name: Any) -> str | None:
    config = apps.get_containing_app_config(str(module_name))
    return config.label if config else None


def _ready_hint(module_name: str) -> str:
    config = apps.get_containing_app_config(module_name)
    if config is None:
        return f"Import {module_name} where your project starts up."
    cls = type(config)
    if cls is AppConfig:
        return (
            f"Add an AppConfig for {config.label} with a ready() method that "
            f"imports {module_name}."
        )
    return f"Import {module_name} in {cls.__module__}.{cls.__qualname__}.ready()."


# --- W001 -------------------------------------------------------------------

_RECEIVER_DECORATORS = frozenset({"receiver", "receiver_on_commit"})


def _is_receiver_decorator(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _RECEIVER_DECORATORS
    return isinstance(func, ast.Attribute) and func.attr in _RECEIVER_DECORATORS


def _receiver_functions(filename: str) -> list[str]:
    try:
        with open(filename, encoding="utf-8") as fh:
            source = fh.read()
    except (OSError, UnicodeDecodeError):
        return []
    if "receiver" not in source:  # also matches receiver_on_commit
        return []
    try:
        tree = ast.parse(source, filename)
    except (SyntaxError, ValueError):
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(_is_receiver_decorator(d) for d in node.decorator_list)
    ]


def _is_test_file(filename: str) -> bool:
    name = os.path.basename(filename)
    return (
        name in ("tests.py", "conftest.py")
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _first_party_modules() -> Iterator[tuple[str, str]]:
    """Yield (module name, filename) for each module of each first-party app."""
    seen: set[str] = set()
    for config in apps.get_app_configs():
        if not is_first_party(config.name):
            continue
        root = os.path.realpath(config.path)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
            )
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                if path in seen or _is_test_file(path):
                    continue
                seen.add(path)
                rel = os.path.relpath(path, root)[:-3].split(os.sep)
                if rel[-1] == "__init__":
                    rel = rel[:-1]
                yield ".".join([config.name, *rel]), path


def check_unimported_receiver_modules(
    app_configs: Iterable[AppConfig] | None = None,
) -> list[checks.CheckMessage]:
    connected = {(r.receiver_module, r.receiver_qualname) for r in registry.RECORDS}
    live = {
        (getattr(item.receiver, "__module__", None), item.path.rpartition(".")[2])
        for item in iter_live_receivers()
    }
    messages = []
    for module_name, filename in _first_party_modules():
        names = _receiver_functions(filename)
        if not names:
            continue
        missing = [
            name
            for name in names
            if (module_name, name) not in connected and (module_name, name) not in live
        ]
        if not missing:
            continue
        where = relative_path(filename)
        if module_name not in sys.modules:
            msg = f"{where} defines receivers that never run: {', '.join(missing)}."
            hint = f"Nothing imports {module_name}. {_ready_hint(module_name)}"
        else:
            msg = f"{where} defines receivers that are not connected: " + ", ".join(
                missing
            )
            hint = (
                f"{module_name} is imported, but these @receiver functions are "
                "not connected. Were they disconnected, or defined conditionally?"
            )
        messages.append(
            checks.Warning(msg, hint=hint, obj=module_name, id="signal_guard.W001")
        )
    return messages


# --- W002 -------------------------------------------------------------------


def check_late_receivers() -> list[checks.CheckMessage]:
    at_startup = {
        (id(r.signal), r.receiver_path) for r in registry.RECORDS if not r.late
    }
    by_receiver: dict[str, list[registry.ConnectRecord]] = defaultdict(list)
    for record in registry.RECORDS:
        if (
            record.late
            and not record.removed
            and (id(record.signal), record.receiver_path) not in at_startup
            and is_first_party(record.receiver_module)
        ):
            by_receiver[record.receiver_path].append(record)

    messages = []
    for path, records in sorted(by_receiver.items()):
        trigger = records[0].triggered_by
        because = f", when {trigger} imports it" if trigger else ""
        messages.append(
            checks.Warning(
                f"{path} is connected only after startup{because}.",
                hint=(
                    "Processes that never run that import, such as Celery "
                    "workers, management commands and some tests, never run "
                    f"this receiver. {_ready_hint(records[0].receiver_module)}"
                ),
                obj=records[0].receiver_module,
                id="signal_guard.W002",
            )
        )
    return messages


# --- W003 -------------------------------------------------------------------


def check_dead_receivers() -> list[checks.CheckMessage]:
    messages = []
    seen: set[tuple[str, str | None]] = set()
    for record in registry.RECORDS:
        # A weak bound method dying with its object is what weak references
        # are for: Django's own model metadata relies on it. A function or
        # lambda dying means its receiver silently stopped working.
        if record.is_bound_method or not record.weak or record.removed:
            continue
        if not record.is_dead:
            continue
        key = (record.receiver_path, record.connected_at)
        if key in seen:
            continue
        seen.add(key)
        where = f" (connected at {record.connected_at})" if record.connected_at else ""
        messages.append(
            checks.Warning(
                f"{record.receiver_path}{where} was garbage-collected and no "
                "longer runs.",
                hint=(
                    "Django holds receivers by weak reference by default, so a "
                    "nested function or lambda disappears silently once nothing "
                    "else refers to it. Define the receiver at module level, or "
                    "connect it with weak=False."
                ),
                obj=record.receiver_module,
                id="signal_guard.W003",
            )
        )
    return messages


# --- W004 -------------------------------------------------------------------


def _identity(receiver: Any) -> tuple[Any, ...]:
    if hasattr(receiver, "__self__") and hasattr(receiver, "__func__"):
        # Bound methods of different objects are different receivers, such as
        # ImageField.update_dimension_fields for two image fields.
        return ("method", id(receiver.__self__), id(receiver.__func__))
    with contextlib.suppress(ValueError):
        receiver = inspect.unwrap(receiver)  # @receiver_on_commit's wrapper
    code = getattr(receiver, "__code__", None)
    if code is None:
        return ("object", id(receiver))
    qualname = getattr(receiver, "__qualname__", "")
    # File, line and name rather than the code object: a closure created twice,
    # or a module imported under two names, is still the same receiver.
    return ("function", code.co_filename, code.co_firstlineno, qualname)


def check_duplicate_receivers() -> list[checks.CheckMessage]:
    groups: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for item in iter_live_receivers():
        key = (item.signal_name, id(item.signal), item.sender_key)
        groups[(*key, _identity(item.receiver))].append(item)

    messages = []
    for items in groups.values():
        if len(items) < 2:
            continue
        first = items[0]
        uids = sorted({str(i.dispatch_uid) for i in items if i.dispatch_uid})
        uid_note = f" (dispatch_uid {', '.join(uids)})" if uids else ""
        module = getattr(first.receiver, "__module__", "") or ""
        messages.append(
            checks.Warning(
                f"{first.path} is connected {len(items)} times to "
                f"{first.signal_name} for sender {first.sender_label}{uid_note}, "
                f"so it runs {len(items)} times per signal.",
                hint=(
                    "Connect it once, or give every connect() the same dispatch_uid."
                ),
                obj=module,
                id="signal_guard.W004",
            )
        )
    return messages
