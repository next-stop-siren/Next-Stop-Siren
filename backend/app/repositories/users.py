"""Local user queries using PostgreSQL's email comparison semantics."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import User


def find_local_user_by_email(session: Session, email: str) -> User | None:
    """Google 전용 계정은 로컬 이메일 중복에서 제외한다."""
    return session.scalar(
        select(User).where(User.password_hash.is_not(None), func.lower(User.email) == func.lower(email))
    )


def add_local_user(session: Session, email: str, password_hash: str) -> User:
    """커밋 없이 flush로 ID와 생성 시각을 확보한다."""
    user = User(email=email, password_hash=password_hash)
    session.add(user)
    session.flush()
    return user
