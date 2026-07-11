import asyncio

from recombee_api_client.api_client import RecombeeClient, Region
from app.core.config import settings

recombee_client = RecombeeClient(
    settings.RECOMBEE_DB_ID,
    settings.RECOMBEE_PRIVATE_TOKEN,
    region=Region.US_WEST
)


async def recombee_send(request):
    """
    Send a Recombee request without blocking the event loop.

    RecombeeClient.send() is synchronous (uses `requests` under the hood),
    so it must run in a worker thread when called from async endpoints.
    """
    return await asyncio.to_thread(recombee_client.send, request)
