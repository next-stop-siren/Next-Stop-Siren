"""Return the public user selected by a verified Bearer access token."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.auth import authenticated_session, current_user_id
from app.api.errors import unauthenticated
from app.repositories.users import find_user_by_id
from app.schemas.auth import MeResponse, PublicUser
from app.schemas.errors import ErrorResponse

router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/me", response_model=MeResponse, responses={code: {"model": ErrorResponse} for code in (401, 503)})
def me(
    user_id: Annotated[int, Depends(current_user_id)],
    response: Response,
    session: Annotated[Session, Depends(authenticated_session)],
) -> MeResponse:
    """토큰에서 확인한 사용자만 조회하고, 비밀번호 해시나 세션 정보는 반환하지 않는다."""
    user = find_user_by_id(session, user_id)
    if user is None:
        raise unauthenticated()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return MeResponse(user=PublicUser.model_validate(user))
