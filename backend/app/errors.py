class DocumentProcessingError(RuntimeError):
    """A document-level error that should remain visible to the user."""


class ProcessingStopped(DocumentProcessingError):
    """Raised cooperatively after the user stops a queued or running PDF job."""
