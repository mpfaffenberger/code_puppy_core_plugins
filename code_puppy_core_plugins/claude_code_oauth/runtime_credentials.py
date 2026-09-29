"""Async credential providers for core's request-time OAuth lifecycle."""

import asyncio

from . import utils


async def current_token():
    return await asyncio.to_thread(utils.get_valid_access_token)


async def refresh_token(*, rejected_token=None):
    return await asyncio.to_thread(
        utils.refresh_access_token, force=True, rejected_token=rejected_token
    )
