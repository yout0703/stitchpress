class StitchPressError(ValueError):
    """An actionable domain failure safe to show in the desktop."""


class ConflictError(StitchPressError):
    """A read dependency changed; the caller must retain its draft."""


class ResourceError(StitchPressError):
    """A required resource is absent, invalid, or exceeds the tested limit."""


class Cancelled(StitchPressError):
    """Explicit cancellation, separate from an encoding or validation failure."""
