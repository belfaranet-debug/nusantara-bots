"""Learning loop — skill-change proposals gated by explicit humans."""

from .learn import (
    CREATE_VERSION,
    DEFAULT_LEARN_FILE,
    DRAFT_STATUSES,
    MAX_DESCRIPTION_CHARS,
    MAX_DRAFT_BYTES,
    REQUIRED_SECTIONS,
    LearnLoop,
    build_distillation_context,
    parse_learn_command,
    validate_draft,
)
from .proposals import (
    AUTO_APPROVAL_REFUSAL,
    DEFAULT_LEARNING_FILE,
    FALLBACK_APPROVAL_ACTION,
    PREFERRED_APPROVAL_ACTION,
    PROPOSAL_STATUSES,
    LearningLoop,
    bump_patch,
)

__all__ = [
    "AUTO_APPROVAL_REFUSAL",
    "CREATE_VERSION",
    "DEFAULT_LEARNING_FILE",
    "DEFAULT_LEARN_FILE",
    "DRAFT_STATUSES",
    "FALLBACK_APPROVAL_ACTION",
    "MAX_DESCRIPTION_CHARS",
    "MAX_DRAFT_BYTES",
    "PREFERRED_APPROVAL_ACTION",
    "PROPOSAL_STATUSES",
    "REQUIRED_SECTIONS",
    "LearningLoop",
    "LearnLoop",
    "build_distillation_context",
    "bump_patch",
    "parse_learn_command",
    "validate_draft",
]
