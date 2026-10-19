from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.auth import find_user
from app.db.models import User
from app.db.session import get_db
from app.utils.security import decode_token

DB = Annotated[AsyncSession, Depends(get_db)]
bearer = HTTPBearer(auto_error=False)


async def current_user(
    db: DB, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    if credentials is None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        user_id = decode_token(credentials.credentials)
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(
            401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    user = await find_user(db, user_id)
    if not user:
        raise HTTPException(401, "User no longer exists")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
