"""SiLA 2 Feature Definition (FDL) parsing.

SiLA 2 is the open lab-automation standard: an instrument exposes Features, each
described by an XML Feature Definition with Commands (with typed, constrained
Parameters and Defined Execution Errors) and Properties. Transport is gRPC.

This module reads FDL into plain Python objects. ``drivers.sila2`` turns those
into an MHS manifest, which is the point: a lab that already has SiLA 2
instruments should get MHS drivers from the Feature Definitions it already has,
without hand-writing a driver per instrument.

Covered: Feature identity and description, Commands, Parameters, Responses,
Basic and List and Constrained data types, MinimalInclusive and
MaximalInclusive constraints, Unit labels, Set constraints, Properties, and
Defined Execution Errors. Not covered yet: Structures, Custom data types,
Metadata, Observable command intermediate responses.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

NS = {"s": "http://www.sila-standard.org"}


@dataclass
class DataType:
    basic: str                      # String, Integer, Real, Boolean, Binary, Date, Time, Timestamp, Any
    is_list: bool = False
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    allowed: list[str] = field(default_factory=list)


@dataclass
class Element:
    identifier: str
    display_name: str
    description: str
    data_type: DataType


@dataclass
class Command:
    identifier: str
    display_name: str
    description: str
    observable: bool
    parameters: list[Element]
    responses: list[Element]
    errors: list[str]


@dataclass
class Property:
    identifier: str
    display_name: str
    description: str
    observable: bool
    data_type: DataType


@dataclass
class Feature:
    identifier: str
    display_name: str
    description: str
    originator: str
    category: str
    version: str
    commands: list[Command]
    properties: list[Property]
    errors: dict[str, str]          # identifier -> description

    @property
    def fully_qualified(self) -> str:
        major = self.version.split(".")[0]
        return f"{self.originator}/{self.category}/{self.identifier}/v{major}"


def _text(el: ET.Element | None, tag: str) -> str:
    if el is None:
        return ""
    found = el.find(f"s:{tag}", NS)
    return (found.text or "").strip() if found is not None else ""


def _data_type(dt: ET.Element) -> DataType:
    basic = dt.find("s:Basic", NS)
    if basic is not None:
        return DataType(basic.text.strip())
    lst = dt.find("s:List/s:DataType", NS)
    if lst is not None:
        inner = _data_type(lst)
        inner.is_list = True
        return inner
    con = dt.find("s:Constrained", NS)
    if con is not None:
        inner = _data_type(con.find("s:DataType", NS))
        cs = con.find("s:Constraints", NS)
        if cs is not None:
            lo, hi = cs.find("s:MinimalInclusive", NS), cs.find("s:MaximalInclusive", NS)
            if lo is not None:
                inner.minimum = float(lo.text)
            if hi is not None:
                inner.maximum = float(hi.text)
            unit = cs.find("s:Unit/s:Label", NS)
            if unit is not None:
                inner.unit = unit.text.strip()
            inner.allowed = [v.text.strip() for v in cs.findall("s:Set/s:Value", NS)]
        return inner
    raise ValueError("unsupported SiLA data type (Structure and DataTypeIdentifier are not covered yet)")


def _element(el: ET.Element) -> Element:
    return Element(_text(el, "Identifier"), _text(el, "DisplayName"), _text(el, "Description"),
                   _data_type(el.find("s:DataType", NS)))


def parse_feature(xml: str) -> Feature:
    root = ET.fromstring(xml)
    if root.tag != f"{{{NS['s']}}}Feature":
        raise ValueError("not a SiLA 2 Feature Definition")
    commands = [Command(
        identifier=_text(c, "Identifier"), display_name=_text(c, "DisplayName"), description=_text(c, "Description"),
        observable=_text(c, "Observable") == "Yes",
        parameters=[_element(p) for p in c.findall("s:Parameter", NS)],
        responses=[_element(r) for r in c.findall("s:Response", NS)],
        errors=[e.text.strip() for e in c.findall("s:DefinedExecutionErrors/s:Identifier", NS)],
    ) for c in root.findall("s:Command", NS)]
    properties = [Property(_text(p, "Identifier"), _text(p, "DisplayName"), _text(p, "Description"),
                           _text(p, "Observable") == "Yes", _data_type(p.find("s:DataType", NS)))
                  for p in root.findall("s:Property", NS)]
    errors = {_text(e, "Identifier"): _text(e, "Description") for e in root.findall("s:DefinedExecutionError", NS)}
    return Feature(_text(root, "Identifier"), _text(root, "DisplayName"), _text(root, "Description"),
                   root.get("Originator", ""), root.get("Category", ""), root.get("FeatureVersion", "1.0"),
                   commands, properties, errors)


def load_feature(path: str | Path) -> Feature:
    return parse_feature(Path(path).read_text(encoding="utf-8"))


class SiLA2ExecutionError(Exception):
    """A Defined Execution Error raised by a SiLA 2 server."""

    def __init__(self, identifier: str, message: str, details: dict | None = None):
        super().__init__(f"{identifier}: {message}")
        self.identifier = identifier
        self.message = message
        self.details = details or {}
