"""Westgard multirule QC, evaluated on the most recent run and its predecessor.

1_3s reject; 2_2s reject (two consecutive beyond 2 SD, same side);
R_4s reject (consecutive runs more than 4 SD apart, opposite sides); 1_2s warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QCStatus:
    status: str                  # pass, warn, fail, none
    violations: list[str] = field(default_factory=list)
    last_z: float | None = None


def westgard(z: list[float]) -> QCStatus:
    if not z:
        return QCStatus("none")
    cur = z[-1]
    v: list[str] = []
    if abs(cur) > 3:
        v.append("1_3s")
    if len(z) >= 2:
        prev = z[-2]
        if (cur > 2 and prev > 2) or (cur < -2 and prev < -2):
            v.append("2_2s")
        if ((cur > 2 and prev < -2) or (cur < -2 and prev > 2)) and abs(cur - prev) > 4:
            v.append("R_4s")
    if v:
        return QCStatus("fail", v, round(cur, 2))
    if abs(cur) > 2:
        return QCStatus("warn", ["1_2s"], round(cur, 2))
    return QCStatus("pass", [], round(cur, 2))


def z_scores(history: list[dict], targets: dict) -> list[float]:
    return [(h["value"] - targets[h["level"]]["mean"]) / targets[h["level"]]["sd"] for h in history]
