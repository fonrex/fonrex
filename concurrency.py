"""Shared boundary between asynchronous orchestration and blocking adapters."""

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")


async def run_sync(
    operation: Callable[Parameters, Result],
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> Result:
    """Run a blocking operation in the default worker pool.

    Context variables are propagated by ``asyncio.to_thread`` and keyword
    arguments remain supported, unlike the lower-level executor API.
    """
    return await asyncio.to_thread(operation, *args, **kwargs)
