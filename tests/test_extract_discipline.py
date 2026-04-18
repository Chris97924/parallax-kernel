"""Discipline tests — parallax core never imports the extract layer or httpx.

Runs each check in a fresh subprocess so the parent test process's already-
imported ``httpx`` (pulled in for other test tooling) cannot poison the
result. Only stdlib; no extra deps.
"""

from __future__ import annotations

import subprocess
import sys


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_core_import_does_not_pull_extract_or_httpx() -> None:
    code = (
        "import parallax, sys;"
        "assert 'parallax.extract' not in sys.modules, 'extract leaked';"
        "assert 'httpx' not in sys.modules, 'httpx leaked';"
        "assert 'anthropic' not in sys.modules, 'anthropic leaked';"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr


def test_ingest_and_retrieve_surface_clean() -> None:
    code = (
        "import parallax.ingest, parallax.retrieve, parallax.events, parallax.index, sys;"
        "assert 'parallax.extract' not in sys.modules, 'extract leaked via ingest';"
        "assert 'httpx' not in sys.modules, 'httpx leaked via core helpers';"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr


def test_extract_is_importable_without_extra() -> None:
    # RawClaim / Provider / extract_claims / extract_and_ingest use only stdlib
    code = (
        "from parallax.extract import RawClaim, Provider, extract_claims, extract_and_ingest;"
        "from parallax.extract.providers.mock import MockProvider;"
        "assert isinstance(MockProvider(), Provider);"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
