"""P1-01: structured JSON logging tests."""

from __future__ import annotations

import io
import json
import logging

from parallax.ingest import ingest_claim, ingest_memory
from parallax.obs.log import JSONFormatter, get_logger


def test_json_formatter_valid_json() -> None:
    fmt = JSONFormatter()
    rec = logging.LogRecord(
        name="parallax.test", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    rec.user_id = "u1"
    out = fmt.format(rec)
    payload = json.loads(out)
    assert payload["msg"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "parallax.test"
    assert payload["user_id"] == "u1"
    assert "ts" in payload


def test_get_logger_is_json_and_idempotent() -> None:
    a = get_logger("parallax.obs.test")
    b = get_logger("parallax.obs.test")
    assert a is b
    assert len([h for h in a.handlers if getattr(h, "_parallax_json", False)]) == 1


def _capture(name: str) -> tuple[logging.Logger, io.StringIO]:
    logger = get_logger(name)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    return logger, buf


def test_ingest_memory_emits_event_line(conn) -> None:
    logger, buf = _capture("parallax.ingest")
    ingest_memory(
        conn, user_id="u1", title="t", summary="s",
        vault_path="v.md", source_id=None,
    )
    logger.handlers[-1].flush()
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]
    assert any(p.get("event") == "ingest_memory" for p in payloads)


def test_ingest_claim_emits_event_line(conn) -> None:
    logger, buf = _capture("parallax.ingest")
    ingest_claim(
        conn, user_id="u1", subject="x", predicate="y", object_="z", source_id=None,
    )
    logger.handlers[-1].flush()
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]
    assert any(p.get("event") == "ingest_claim" for p in payloads)
