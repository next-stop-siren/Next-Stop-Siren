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


def find_user_for_update(session: Session, user_id: int) -> User | None:
    """사용자를 잠그고 기존 ORM 객체도 최신 DB 값으로 갱신한다."""
    return session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )


def delete_user(session: Session, user: User) -> None:
    """사용자 삭제를 예약하며 커밋은 호출자가 담당한다."""
    session.delete(user)
