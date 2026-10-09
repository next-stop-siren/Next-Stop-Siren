"""The verified user ID that protected routes trust instead of any client-supplied value."""

from app.api.errors import unauthenticated


def current_user_id() -> int:
    """Return the verified principal's user ID.

    Access token verification belongs to the authentication issues. Until it is
    connected here, no request is authenticated and every protected route answers 401.
    """
    raise unauthenticated()
