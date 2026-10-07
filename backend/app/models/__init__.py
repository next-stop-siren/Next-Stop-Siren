"""Import application model modules here so initialization sees their tables.

Product models are introduced by their owning issues; this registry is empty now.
"""


def register_models() -> None:
    """Import model modules here as they are added to the application."""
    from app.models import conversation  # noqa: F401
