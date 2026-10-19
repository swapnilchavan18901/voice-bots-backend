from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.db.models import Membership, Organization, User
from app.types import AuthOut, UserOut
from app.utils.security import DUMMY_HASH, access_token, password_hasher


async def register(db, data):
    user = User(
        email=data.email,
        name=data.name,
        password_hash=await run_in_threadpool(password_hasher.hash, data.password),
    )
    db.add(user)
    try:
        await db.flush()
        org = Organization(name=data.email.split("@")[0][:100], personal_for_user_id=user.id)
        db.add(org)
        await db.flush()
        db.add(Membership(organization_id=org.id, user_id=user.id, role="owner"))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Email already registered") from None
    return AuthOut(access_token=access_token(user.id), user=UserOut.model_validate(user))


async def login(db, data):
    user = await db.scalar(select(User).where(User.email == data.email))
    valid = await run_in_threadpool(
        password_hasher.verify, data.password, user.password_hash if user else DUMMY_HASH
    )
    if not user or not valid:
        raise HTTPException(401, "Invalid email or password")
    return AuthOut(access_token=access_token(user.id), user=UserOut.model_validate(user))


async def find_user(db, user_id):
    return await db.get(User, user_id)
