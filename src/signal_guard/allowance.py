"""A base for context managers that allow something, with a reason."""

from __future__ import annotations

from contextlib import ContextDecorator
from contextvars import ContextVar
from typing import ClassVar, TypeVar

A = TypeVar("A", bound="Allowance")


class Allowance(ContextDecorator):
    """Entered allowances live in a context variable, one per subclass.

    State lives only in the context variable, never on the instance, so one
    instance can be entered from several threads or tasks at once (as a
    decorator on a view, say) without them interfering.
    """

    active: ClassVar[ContextVar[tuple[Allowance, ...]]]

    def __init__(self, *, reason: str, example: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"{type(self).__name__}() needs a non-empty reason, for example "
                f"reason={example!r}."
            )
        self.reason = reason

    @classmethod
    def entered(cls: type[A]) -> tuple[A, ...]:
        return cls.active.get()  # type: ignore[return-value]

    def __enter__(self: A) -> A:
        active = type(self).active
        active.set((*active.get(), self))
        return self

    def __exit__(self, *exc_info: object) -> None:
        active = type(self).active
        current = active.get()
        for index in range(len(current) - 1, -1, -1):
            if current[index] is self:
                active.set(current[:index] + current[index + 1 :])
                return

    async def __aenter__(self: A) -> A:
        return self.__enter__()

    async def __aexit__(self, *exc_info: object) -> None:
        self.__exit__(*exc_info)
