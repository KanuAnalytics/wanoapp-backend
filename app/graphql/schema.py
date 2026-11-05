"""
GraphQL schema definition

app/graphql/schema.py

"""
import strawberry
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, Request, WebSocket
from graphql.validation import NoSchemaIntrospectionCustomRule
from strawberry.extensions import (
    AddValidationRules,
    MaxAliasesLimiter,
    MaxTokensLimiter,
    QueryDepthLimiter,
)
from strawberry.fastapi import GraphQLRouter
from typing import Optional, Dict, Any

from app.core.config import settings
from app.core.database import get_database
from app.core.security import decode_access_token
from app.graphql.queries import Query
from app.graphql.mutations import Mutation
from app.graphql.subscriptions import Subscription


# Custom context type
@strawberry.type
class Context:
    user_id: Optional[str] = None
    request: Optional[Request] = None


async def _authenticated_user_id(token: str) -> Optional[str]:
    """Return the token's user id if it is valid, the user is active and the
    token was not revoked by a password change; otherwise None."""
    payload = decode_access_token(token)
    if not payload:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        oid = ObjectId(str(sub))
    except (InvalidId, TypeError):
        return None
    user = await get_database().users.find_one(
        {"_id": oid, "is_active": True}, {"password_changed_at": 1}
    )
    if not user:
        return None
    try:
        from app.api.deps import _ensure_token_not_revoked
        _ensure_token_not_revoked(user, token)
    except HTTPException:
        return None
    except ImportError:
        pass
    return str(oid)


async def get_context(request: Request = None, ws: WebSocket = None) -> Dict[str, Any]:
    """Get context with user authentication (HTTP requests and websockets)."""
    conn = request if request is not None else ws
    context: Dict[str, Any] = {"request": conn}

    auth_header = conn.headers.get("Authorization") if conn is not None else None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        user_id = await _authenticated_user_id(token)
        if user_id:
            context["user_id"] = user_id

    return context


extensions = [
    QueryDepthLimiter(max_depth=10),
    MaxAliasesLimiter(max_alias_count=15),
    MaxTokensLimiter(max_token_count=2000),
]
if not settings.DEBUG:
    extensions.append(AddValidationRules([NoSchemaIntrospectionCustomRule]))

# Create the schema
schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    extensions=extensions,
)

# Create GraphQL router
graphql_app = GraphQLRouter(
    schema,
    context_getter=get_context,
    graphql_ide="graphiql" if settings.DEBUG else None,
)
