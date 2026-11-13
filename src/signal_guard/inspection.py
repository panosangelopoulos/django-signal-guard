"""List every live receiver with readable names for its signal and sender."""

from __future__ import annotations

import ast
import contextlib
import functools
import os
import site
import sys
import sysconfig
import types
import weakref
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db.models.signals import ModelSignal
from django.dispatch import Signal
from django.dispatch.dispatcher import NONE_ID

from . import registry
from .conf import get_config
from .receivers import describe


@dataclass(frozen=True)
class LiveReceiver:
    signal: Signal
    signal_name: str
    sender_key: Any
    sender_label: str
    receiver: Any
    path: str
    location: str | None
    dispatch_uid: str | None
    first_party: bool
    sender_first_party: bool

    @property
    def lock_line(self) -> str:
        parts = [self.signal_name, self.sender_label, self.path]
        if self.dispatch_uid:
            parts.append(f"uid={self.dispatch_uid}")
        return " | ".join(parts)


# --- first party ------------------------------------------------------------


@functools.cache
def _third_party_roots() -> tuple[str, ...]:
    paths = {sysconfig.get_paths().get(k) for k in ("stdlib", "platstdlib")}
    paths |= {sysconfig.get_paths().get(k) for k in ("purelib", "platlib")}
    with contextlib.suppress(AttributeError):  # missing in some embedded Pythons
        paths |= set(site.getsitepackages())
    paths.add(site.getusersitepackages())
    return tuple(sorted(os.path.realpath(p) for p in paths if p))


def is_first_party(module_name: str) -> bool:
    """True if ``module_name`` is the project's own code, not a dependency.

    This package never is, even when installed in editable mode.
    """
    if module_name == "signal_guard" or module_name.startswith("signal_guard."):
        return False
    explicit = get_config().first_party
    if explicit is not None:
        return any(
            module_name == p or module_name.startswith(p + ".") for p in explicit
        )
    if module_name == "django" or module_name.startswith("django."):
        return False
    module = sys.modules.get(module_name)
    filename = getattr(module, "__file__", None)
    if not filename:
        return False
    real = os.path.realpath(filename)
    return not any(real.startswith(root + os.sep) for root in _third_party_roots())


# --- names ------------------------------------------------------------------


@functools.cache
def _assigned_names(filename: str) -> frozenset[str]:
    """Top-level names a module assigns (as opposed to imports)."""
    try:
        with open(filename, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return frozenset()
    names: set[str] = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return frozenset(names)


def _module_signals() -> dict[int, tuple[Signal, list[tuple[str, str]]]]:
    """Every Signal held in a module global, with each (module, name) holding it."""
    found: dict[int, tuple[Signal, list[tuple[str, str]]]] = {}
    for module_name, module in list(sys.modules.items()):
        if not isinstance(module, types.ModuleType):
            continue
        for attr, value in list(vars(module).items()):
            # type(), not isinstance(): isinstance() asks a LazyObject for its
            # __class__, which would set up lazy storages, settings and so on.
            if issubclass(type(value), Signal):
                found.setdefault(id(value), (value, []))[1].append((module_name, attr))
    return found


def _pick_name(candidates: list[tuple[str, str]]) -> str:
    """Prefer the module that defines the signal over the ones importing it."""

    def rank(candidate: tuple[str, str]) -> tuple[bool, bool, int, str, str]:
        module_name, attr = candidate
        return (
            "signals" not in module_name,
            not module_name.startswith("django."),
            len(module_name),
            module_name,
            attr,
        )

    ordered = sorted(candidates, key=rank)
    for module_name, attr in ordered:
        filename = getattr(sys.modules.get(module_name), "__file__", None)
        if filename and attr in _assigned_names(filename):
            return f"{module_name}.{attr}"
    module_name, attr = ordered[0]
    return f"{module_name}.{attr}"


def all_signals() -> dict[Signal, str]:
    """Every signal reachable from a module or seen by the registry, named."""
    named: dict[Signal, str] = {}
    for signal, candidates in _module_signals().values():
        named[signal] = _pick_name(candidates)
    for record in registry.RECORDS:
        signal = record.signal
        if signal is not None and signal not in named:
            named[signal] = f"<unnamed {type(signal).__name__}>"
    return named


def _sender_labels() -> dict[Any, tuple[str, bool]]:
    """Map Django's sender ids to (label, sender is first-party)."""
    labels: dict[Any, tuple[str, bool]] = {NONE_ID: ("*", False)}
    for record in registry.RECORDS:
        labels.setdefault(record.sender_key, (record.sender_label, False))
    for config in apps.get_app_configs():
        labels[id(config)] = (f"app:{config.label}", is_first_party(config.name))
    for model in apps.get_models(include_auto_created=True):
        labels[id(model)] = (model._meta.label, is_first_party(model.__module__))
    return labels


# --- receivers --------------------------------------------------------------


def _resolve(stored: Any) -> Any:
    if isinstance(stored, weakref.ReferenceType):
        return stored()
    return stored


def iter_live_receivers() -> Iterator[LiveReceiver]:
    senders = _sender_labels()
    for signal, signal_name in all_signals().items():
        with signal.lock:
            entries = list(signal.receivers)
        for entry in entries:
            (receiver_key, sender_key), stored = entry[0], entry[1]
            receiver = _resolve(stored)
            if receiver is None:
                continue
            described = describe("", receiver)
            target = getattr(receiver, "__func__", receiver)
            module = getattr(target, "__module__", None) or ""
            label, sender_first_party = senders.get(
                sender_key, ("<unknown sender>", False)
            )
            yield LiveReceiver(
                signal=signal,
                signal_name=signal_name,
                sender_key=sender_key,
                sender_label=label,
                receiver=receiver,
                path=described.path,
                location=described.location,
                dispatch_uid=receiver_key if isinstance(receiver_key, str) else None,
                first_party=is_first_party(module),
                sender_first_party=sender_first_party,
            )


def is_mapped(item: LiveReceiver) -> bool:
    """Whether a receiver belongs in the signal map by default.

    Your own receivers, plus third-party receivers that run on your models:
    connected to one of them, or to every sender of a model signal.
    """
    if item.first_party or item.sender_first_party:
        return True
    return item.sender_label == "*" and isinstance(item.signal, ModelSignal)


def load_urlconf() -> None:
    """Import the URLconf, as a running server would, before inspecting."""
    if not getattr(settings, "ROOT_URLCONF", None):
        return
    try:
        from django.urls import get_resolver

        get_resolver().url_patterns  # noqa: B018 - imports views and their imports
    except Exception:  # Django's own URL checks report these.
        pass
