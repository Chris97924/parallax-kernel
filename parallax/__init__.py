"""Parallax — canonical knowledge-base package.

Public API re-exports for Parallax Kernel. Import everything you need from the
package root:

    from parallax import (
        ingest_memory, ingest_claim,
        memories_by_user, claims_by_user, claims_by_subject,
        memory_by_content_hash, claim_by_content_hash,
        Source, Memory, Claim, Event,
    )
"""

from parallax.ingest import ingest_claim, ingest_memory
from parallax.retrieve import (
    claim_by_content_hash,
    claims_by_subject,
    claims_by_user,
    memories_by_user,
    memory_by_content_hash,
)
from parallax.sqlite_store import Claim, Event, Memory, Source

__version__ = "0.1.1"

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
    "__version__",
]
