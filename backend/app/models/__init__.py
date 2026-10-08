"""Register application models for schema initialization."""

from importlib import import_module


def register_models() -> None:
    """Import product models; repeated calls reuse the same metadata."""
    for module in ("user", "auth_identity", "refresh_session", "conversation"):
        import_module(f"app.models.{module}")
