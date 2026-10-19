"""Shared helpers for applications built on MHS."""

from __future__ import annotations

from ..mhs_shim import Decision, Manifest


def validate(manifest: Manifest, procedure: str, args: dict) -> Decision | None:
    """Reject unknown procedures and missing required parameters before any gate runs."""
    proc = manifest.procedure(procedure)
    if proc is None:
        return Decision.block("unknown_procedure", f"{manifest.device_id} has no procedure {procedure!r}")
    missing = [p.name for p in proc.params if p.required and p.name not in args]
    if missing:
        return Decision.block("missing_param", f"missing required parameters {missing}")
    return None
