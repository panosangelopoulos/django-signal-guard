"""Guesses about what a receiver does, read from its source code.

These are heuristics: they look at the shape of the code, not at what it does
at run time, so they only recognise a few common patterns and say "no" when
in doubt.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import inspect
import textwrap
from collections.abc import Callable
from types import CodeType
from typing import Any

_create_only: dict[CodeType, bool] = {}


def _function(receiver: Callable[..., Any]) -> Any:
    target: Any = receiver
    while isinstance(target, functools.partial):
        target = target.func
    target = getattr(target, "__func__", target)  # bound method -> function
    with contextlib.suppress(ValueError):  # a cycle of __wrapped__
        target = inspect.unwrap(target)  # @receiver_on_commit's wrapper
    return target


def is_create_only(receiver: Callable[..., Any]) -> bool:
    """True if a post_save receiver only does something when ``created``.

    Recognises a body that starts with ``if not created: return`` (or
    ``if not created or ...: return``), and a body that is a single
    ``if created:`` (or ``if created and ...:``) with no ``else``. A docstring
    first is fine.
    """
    func = _function(receiver)
    code = getattr(func, "__code__", None)
    if not isinstance(code, CodeType):
        return False
    try:
        return _create_only[code]
    except KeyError:
        pass
    result = _read_create_only(func)
    _create_only[code] = result
    return result


def _read_create_only(func: Any) -> bool:
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return False
    if not tree.body or not isinstance(
        tree.body[0], ast.FunctionDef | ast.AsyncFunctionDef
    ):
        return False  # a lambda, or source we cannot place
    node = tree.body[0]
    arguments = node.args
    names = {a.arg for a in (*arguments.posonlyargs, *arguments.args)}
    names |= {a.arg for a in arguments.kwonlyargs}
    if "created" not in names:
        return False

    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # the docstring
    if not body or not isinstance(body[0], ast.If) or body[0].orelse:
        return False
    first = body[0]
    if _implies_not_created(first.test) and _only_returns(first.body):
        return True
    return len(body) == 1 and _requires_created(first.test)


def _is_created(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "created"


def _compares_created_to(node: ast.expr, value: bool) -> bool:
    """``created is True``, ``created == False`` and the like."""
    return (
        isinstance(node, ast.Compare)
        and _is_created(node.left)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Is | ast.Eq)
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value is value
    )


def _requires_created(test: ast.expr) -> bool:
    """True if ``test`` can only be true when ``created`` is true."""
    if _is_created(test) or _compares_created_to(test, True):
        return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_requires_created(value) for value in test.values)
    return False


def _implies_not_created(test: ast.expr) -> bool:
    """True if ``test`` is always true when ``created`` is false."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _is_created(test.operand)
    if _compares_created_to(test, False):
        return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_implies_not_created(value) for value in test.values)
    return False


def _only_returns(body: list[ast.stmt]) -> bool:
    return len(body) == 1 and isinstance(body[0], ast.Return)
