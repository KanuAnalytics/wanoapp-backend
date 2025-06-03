"""
GraphQL schema definition

app/graphql/schema.py

"""
import strawberry
from strawberry.fastapi import GraphQLRouter
from strawberry.types import Info
from app.graphql.queries import Query
from app.graphql.mutations import Mutation
from app.graphql.subscriptions import Subscription
from app.core.security import decode_access_token
from fastapi import Depends, Request
from typing import Optional, Dict, Any

# Custom context type
@strawberry.type
class Context:
    user_id: Optional[str] = None
    request: Optional[Request] = None

# Custom context getter for authentication
async def get_context(request: Request) -> Dict[str, Any]:
    """Get context with user authentication"""
    context = {"request": request}
    
    # Extract token from Authorization header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        payload = decode_access_token(token)
        if payload:
            context["user_id"] = payload.get("sub")
    
    return context

# Create the schema
schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription
)

# Create GraphQL router
graphql_app = GraphQLRouter(
    schema,
    context_getter=get_context,
    graphql_ide="graphiql"  # Enable GraphiQL interface
)