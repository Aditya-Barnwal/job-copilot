"""Generated writing — cover letters, application/follow-up emails — grounded in
confirmed résumé facts and shown to the user before use (docs/WRITING_GUIDE.md)."""
from .common import Draft, WritingError, REFUSE_NO_FACTS  # noqa: F401
from .cover_letter import generate_cover_letter  # noqa: F401
from .email import generate_email  # noqa: F401
