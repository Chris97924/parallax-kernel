"""MEMORY_ROUTER package — Lane D-1 contract freeze.

Re-exports all public names from the router sub-modules. Imports are lazy
via __getattr__ so that `import parallax.router.types` does not drag in
parallax.retrieval or parallax.server (isolation requirement from US-001 AC).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    # types
    "QueryType",
    "MappingState",
    "FieldCandidate",
    # contracts
    "ArbitrationDecision",
    "QueryRequest",
    "IngestRequest",
    "IngestResult",
    "BackfillRequest",
    "BackfillReport",
    "HealthReport",
    "RetrievalEvidence",
    # ports
    "QueryPort",
    "IngestPort",
    "InspectPort",
    "BackfillPort",
    # mock
    "MockMemoryRouter",
    # crosswalk
    "CROSSWALK_SEED",
    "resolve",
    "UnroutableQueryError",
    "seed_hash",
    # config
    "MEMORY_ROUTER",
    "is_router_enabled",
]

_LAZY: dict[str, tuple[str, str]] = {
    # name -> (module, attr)
    "QueryType": ("parallax.router.types", "QueryType"),
    "MappingState": ("parallax.router.types", "MappingState"),
    "FieldCandidate": ("parallax.router.types", "FieldCandidate"),
    "ArbitrationDecision": ("parallax.router.contracts", "ArbitrationDecision"),
    "QueryRequest": ("parallax.router.contracts", "QueryRequest"),
    "IngestRequest": ("parallax.router.contracts", "IngestRequest"),
    "IngestResult": ("parallax.router.contracts", "IngestResult"),
    "BackfillRequest": ("parallax.router.contracts", "BackfillRequest"),
    "BackfillReport": ("parallax.router.contracts", "BackfillReport"),
    "HealthReport": ("parallax.router.contracts", "HealthReport"),
    "RetrievalEvidence": ("parallax.router.contracts", "RetrievalEvidence"),
    "QueryPort": ("parallax.router.ports", "QueryPort"),
    "IngestPort": ("parallax.router.ports", "IngestPort"),
    "InspectPort": ("parallax.router.ports", "InspectPort"),
    "BackfillPort": ("parallax.router.ports", "BackfillPort"),
    "MockMemoryRouter": ("parallax.router.mock_adapter", "MockMemoryRouter"),
    "CROSSWALK_SEED": ("parallax.router.crosswalk_seed", "CROSSWALK_SEED"),
    "resolve": ("parallax.router.crosswalk_seed", "resolve"),
    "UnroutableQueryError": ("parallax.router.crosswalk_seed", "UnroutableQueryError"),
    "seed_hash": ("parallax.router.crosswalk_seed", "seed_hash"),
    "MEMORY_ROUTER": ("parallax.router.config", "MEMORY_ROUTER"),
    "is_router_enabled": ("parallax.router.config", "is_router_enabled"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        mod_name, attr = _LAZY[name]
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
