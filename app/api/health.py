from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/", include_in_schema=False)
@router.head("/", include_in_schema=False)
@router.get("/health")
async def health():
    return {"status": "ok"}
