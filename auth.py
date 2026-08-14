"""
Single shared-secret auth. The agent is only ever called by the Render
backend (never directly by a browser), so one static bearer token is
enough — no user accounts at this layer.
"""
import hmac
from fastapi import Header, HTTPException, status

from config import AGENT_TOKEN


async def require_agent_token(authorization: str = Header(default="")):
    if not AGENT_TOKEN:
        # Fail closed: an agent with no token configured refuses everything
        # rather than silently running open.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Agent token not configured on this VM",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    if not hmac.compare_digest(token, AGENT_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
