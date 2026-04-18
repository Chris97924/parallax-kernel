"""ClaudeSubprocessProvider — shells out to the local ``claude`` CLI.

Useful when the caller already has Claude Code authenticated locally and
wants to avoid configuring an API key. Graceful on failure: any subprocess
error, timeout, or JSON parse error yields an empty list (never raises),
so the shadow write path can stay blameless.
"""

from __future__ import annotations

import logging
import subprocess

from parallax.extract.extractor import render_prompt
from parallax.extract.providers._parse import parse_claims_json
from parallax.extract.types import RawClaim

__all__ = ["ClaudeSubprocessProvider"]

logger = logging.getLogger(__name__)


class ClaudeSubprocessProvider:
    def __init__(self, *, cmd: str = "claude", timeout: float = 60.0) -> None:
        self.cmd = cmd
        self.timeout = timeout

    def extract_claims(self, text: str) -> list[RawClaim]:
        if not text or not text.strip():
            return []
        prompt = render_prompt(text)
        try:
            completed = subprocess.run(
                [self.cmd, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Claude subprocess invocation failed: %s", exc)
            return []
        if completed.returncode != 0:
            logger.warning(
                "Claude CLI exited %d: %s", completed.returncode, completed.stderr[:200]
            )
            return []
        return parse_claims_json(completed.stdout)
