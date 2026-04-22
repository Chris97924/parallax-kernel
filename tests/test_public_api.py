"""Tests for parallax public API surface.

Guards against silent drift where __all__ lists a name but the import
was removed or the re-export was dropped.
"""

from __future__ import annotations

import importlib


class TestPublicAPI:
    def test_version_matches_pyproject(self) -> None:
        import pathlib
        import tomllib

        import parallax

        pyproject = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
        assert parallax.__version__ == declared

    def test_every_all_name_importable_and_not_none(self) -> None:
        mod = importlib.import_module("parallax")
        missing: list[str] = []
        for name in mod.__all__:
            obj = getattr(mod, name, None)
            if obj is None:
                missing.append(name)
        assert missing == [], f"public names missing or None: {missing}"

    def test_v040_additions_importable_directly(self) -> None:
        from parallax import (  # noqa: F401
            BackfillSummary,
            MigrationPlan,
            MigrationStep,
            ReplaySummary,
            applied_versions,
            backfill_creation_events,
            content_hash,
            migrate_down_to,
            migrate_to_latest,
            migration_plan,
            normalize,
            pending,
            reaffirm,
            replay_events,
        )

    def test_legacy_names_still_importable(self) -> None:
        from parallax import (  # noqa: F401
            Claim,
            Event,
            Memory,
            Source,
            build_session_reminder,
            claim_by_content_hash,
            ingest_claim,
            ingest_memory,
            rebuild_index,
            record_event,
        )
