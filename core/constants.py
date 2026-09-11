"""Fixed, ordered constant lists shared across services."""

# The client-project SDLC pipeline (project-progress-backend-spec.md section 1).
# Every project is seeded with all six project_stages rows at creation, in this
# order, so the app always renders the full pipeline with no client-side list.
SDLC_STAGES: list[tuple[str, str]] = [
    ("requirements", "Requirements"),
    ("design", "Design"),
    ("development", "Development"),
    ("testing", "Testing"),
    ("deployment", "Deployment"),
    ("maintenance", "Maintenance"),
]
SDLC_STAGE_KEYS = [key for key, _ in SDLC_STAGES]
SDLC_STAGE_LABELS = dict(SDLC_STAGES)
