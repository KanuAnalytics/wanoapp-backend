from recombee_api_client.api_client import RecombeeClient, Region
from app.core.config import settings

recombee_client = RecombeeClient(
    settings.RECOMBEE_DB_ID,
    settings.RECOMBEE_PRIVATE_TOKEN,
    region=Region.US_WEST
)
