"""Test helpers grouped by the modules and behaviors they support."""


class AllowAllAuthority:  # pylint: disable=too-few-public-methods
    """Admit work in tests that do not exercise run ownership or cancellation."""

    def require_new_work(self) -> None:
        """Allow the next model or tool operation."""
