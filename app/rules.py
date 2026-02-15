from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml


@dataclass(frozen=True)
class SolverPass:
    # limite massimo di presenze di Saporito in N (lun-ven)
    max_saporito_in_N: int
    # tolleranza per bilanciamento delle reperibilità O (differenza max tra core)
    o_balance_tolerance: int


@dataclass(frozen=True)
class Rules:
    tue_m_pair: str
    passes: List[SolverPass]
    weight_deluca: int
    weight_saporito: int


DEFAULT = Rules(
    tue_m_pair="Vizzari / Virga",
    # se infeasible con le indisponibilità, proviamo progressivamente a consentire
    # più presenze di Saporito in N e una tolleranza leggermente maggiore su O.
    passes=[SolverPass(1, 1), SolverPass(2, 2), SolverPass(3, 3)],
    weight_deluca=200,
    weight_saporito=1000,
)


def load_rules(path: Path) -> Rules:
    if not path.exists():
        return DEFAULT
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return DEFAULT

    try:
        tue = str((raw.get("cells") or {}).get("tue_m_pair") or DEFAULT.tue_m_pair)

        passes_raw = ((raw.get("solver") or {}).get("passes") or [])
        passes: List[SolverPass] = []
        for p in passes_raw:
            if not isinstance(p, dict):
                continue
            passes.append(
                SolverPass(
                    max_saporito_in_N=int(p.get("max_saporito_in_N", 1)),
                    o_balance_tolerance=int(p.get("o_balance_tolerance", 1)),
                )
            )
        if not passes:
            passes = DEFAULT.passes

        w = raw.get("objective_weights") or {}
        weight_deluca = int(w.get("de_luca", DEFAULT.weight_deluca))
        weight_saporito = int(w.get("saporito", DEFAULT.weight_saporito))

        return Rules(
            tue_m_pair=tue,
            passes=passes,
            weight_deluca=weight_deluca,
            weight_saporito=weight_saporito,
        )
    except Exception:
        return DEFAULT
