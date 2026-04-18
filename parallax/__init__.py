"""Parallax — canonical knowledge-base package.

Public API re-exports for Parallax Kernel. Import everything you need from the
package root:

    from parallax import (
        ingest_memory, ingest_claim,
        memories_by_user, claims_by_user, claims_by_subject,
        memory_by_content_hash, claim_by_content_hash,
        Source, Memory, Claim, Event,
        record_event, record_memory_reaffirmed, record_claim_state_changed,
        is_allowed_transition, rebuild_index,
    )
"""

from parallax.events import (
    record_claim_state_changed,
    record_event,
    record_memory_reaffirmed,
)
from parallax.index import rebuild_index
from parallax.ingest import ingest_claim, ingest_memory
from parallax.introspection import ParallaxInfo, parallax_info
from parallax.retrieve import (
    claim_by_content_hash,
    claims_by_subject,
    claims_by_user,
    memories_by_user,
    memory_by_content_hash,
)
from parallax.sqlite_store import Claim, Event, Memory, Source
from parallax.telemetry import health
from parallax.transitions import (
    CLAIM_TRANSITIONS,
    DECISION_TRANSITIONS,
    MEMORY_TRANSITIONS,
    SOURCE_TRANSITIONS,
    is_allowed_transition,
)
from parallax.validators import (
    DECISION_TARGET_KINDS,
    VALID_TARGET_KINDS,
    TargetKind,
    target_ref_exists,
)

__version__ = "0.1.5"

__all__ = [
    "ingest_memory",
    "ingest_claim",
    "memories_by_user",
    "claims_by_user",
    "claims_by_subject",
    "memory_by_content_hash",
    "claim_by_content_hash",
    "Source",
    "Memory",
    "Claim",
    "Event",
    "parallax_info",
    "ParallaxInfo",
    "health",
    "target_ref_exists",
    "VALID_TARGET_KINDS",
    "DECISION_TARGET_KINDS",
    "TargetKind",
    "record_event",
    "record_memory_reaffirmed",
    "record_claim_state_changed",
    "is_allowed_transition",
    "MEMORY_TRANSITIONS",
    "CLAIM_TRANSITIONS",
    "SOURCE_TRANSITIONS",
    "DECISION_TRANSITIONS",
    "rebuild_index",
    "__version__",
]
