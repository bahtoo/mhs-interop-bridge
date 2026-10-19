"""A minimal stand-in for the Model Hardware Standard (MHS) runtime.

MHS is the bus. In Anthropic's public description it provides the standard
driver (read and write primitives), device discovery in a standard format,
natural-language tags that generate a reference file, device-level safety
limits, a shared state dictionary, orchestration across devices, and access
for agents over MCP, a CLI and code files.

The MHS SDK is in a waitlisted research preview and is not public. This package
mirrors only the publicly described shape so that the drivers, evals and
cookbooks in this repository can run today. It is deliberately small. It is not
the MHS specification, it is not an alternative to MHS, and every part of it is
meant to be deleted once the real SDK can be imported.

Anything here that goes beyond the public description is marked PROPOSAL: an
idea this repository would offer back to the standard, not a claim about it.
"""

from .manifest import Manifest, Param, Procedure, State
from .runtime import Decision, Driver, Runtime

__all__ = ["Decision", "Driver", "Manifest", "Param", "Procedure", "Runtime", "State"]
