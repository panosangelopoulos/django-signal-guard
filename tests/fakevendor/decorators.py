"""A decorator a vendor package gives you for your receivers."""

import functools


def skip_raw(func):
    """Skip the receiver while loaddata saves fixtures."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if kwargs.get("raw"):
            return None
        return func(*args, **kwargs)

    return wrapper
