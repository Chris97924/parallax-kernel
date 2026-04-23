# Parallax Exchange Profile for DPKG

**Version:** 0.1.0  
**Status:** Provisional  
**Date:** 2026-04-22

## Purpose

This document defines Parallax's adapter-level exchange profile referenced by
DPKG `spec/reserved-namespaces.md`.

DPKG treats `parallax:*` keys as opaque and excludes them from identity
(`content_hash`). This file defines Parallax-side meaning for keys that may be
carried through DPKG payloads.

## Current Profile

Parallax currently does not require any `parallax:*` key for correctness.

The following key is reserved for optional adapter hints:

| Key | Type | Meaning | Required |
|---|---|---|---|
| `parallax:aggregation_level` | string | Optional hint describing source aggregation granularity used by Parallax import/export tooling. | No |

Unknown `parallax:*` keys MUST be preserved by transport and MAY be ignored by
current Parallax runtime unless explicitly documented in a future profile
revision.

## Compatibility Notes

1. `parallax:*` keys are metadata-only and MUST NOT be interpreted as canonical
   DPKG identity fields.
2. Producers SHOULD avoid embedding secrets in `parallax:*` keys because they
   may be stored and echoed by downstream tools.
3. Future profile revisions may add new optional keys without changing DPKG
   core spec version.
