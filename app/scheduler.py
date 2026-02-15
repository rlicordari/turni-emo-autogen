from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .config import (
    ALL_DOCTORS,
    CORE_DOCTORS,
    MON, TUE, WED, THU, FRI, SAT, SUN,
)

try:
    from ortools.sat.python import cp_model  # type: ignore
    _HAS_ORTOOLS = True
except Exception:
    cp_model = None  # type: ignore
    _HAS_ORTOOLS = False

from pathlib import Path
from .rules import load_rules, Rules


@dataclass
class SolveResult:
    ok: bool
    assignment: Dict[dt.date, Dict[str, str]]
    log: str


def _month_dates(year: int, month: int) -> List[dt.date]:
    last = calendar.monthrange(year, month)[1]
    return [dt.date(year, month, d) for d in range(1, last + 1)]


def _weekends(dates: List[dt.date]) -> List[Tuple[dt.date, dt.date]]:
    s = set(dates)
    out = []
    for d in dates:
        if d.weekday() == SAT:
            sun = d + dt.timedelta(days=1)
            if sun in s:
                out.append((d, sun))
    return out


def _fridays(dates: List[dt.date]) -> List[dt.date]:
    return [d for d in dates if d.weekday() == FRI]


def _thursdays(dates: List[dt.date]) -> List[dt.date]:
    return [d for d in dates if d.weekday() == THU]


def _first_following_weekend_for_friday(friday: dt.date, weekends: List[Tuple[dt.date, dt.date]]) -> Optional[Tuple[dt.date, dt.date]]:
    sat = friday + dt.timedelta(days=1)
    for w in weekends:
        if w[0] == sat:
            return w
    return None


def _is_available(unavailability: Dict[str, Set[dt.date]], name: str, d: dt.date) -> bool:
    return d not in unavailability.get(name, set())


def _precheck(year: int, month: int, unavailability: Dict[str, Set[dt.date]]) -> Optional[str]:
    dates = _month_dates(year, month)
    for d in dates:
        if d.weekday() == TUE:
            if d in unavailability.get("Vizzari", set()) or d in unavailability.get("Virga", set()):
                return f"Infeasible: il martedì {d.strftime('%d/%m/%Y')} richiede sempre Vizzari+Virga in M, ma uno dei due è indisponibile."
    for d in _thursdays(dates):
        if (d in unavailability.get("Carciotto", set())) and (d in unavailability.get("Giusti", set())):
            return f"Infeasible: il giovedì {d.strftime('%d/%m/%Y')} la colonna J richiede Carciotto o Giusti, ma sono entrambi indisponibili."
    return None


def solve_month(year: int, month: int, unavailability: Dict[str, Set[dt.date]], time_limit_seconds: int = 10) -> SolveResult:
    """Entry point. Uses OR-Tools CP-SAT if available; otherwise a greedy fallback."""
    rules = load_rules(Path(__file__).resolve().parents[1] / "Regole_Emodinamica.yml")
    msg = _precheck(year, month, unavailability)
    if msg:
        return SolveResult(False, {}, msg)

    if _HAS_ORTOOLS:
        return _solve_month_cpsat(year, month, unavailability, rules=rules, time_limit_seconds=time_limit_seconds)

    # Fallback (dev/local): no ortools installed.
    return _solve_month_greedy(year, month, unavailability, rules=rules)


# --------------------------------------------------------------------------------------
# CP-SAT solver (preferred)
# --------------------------------------------------------------------------------------

def _solve_month_cpsat(year: int, month: int, unavailability: Dict[str, Set[dt.date]], rules: Rules, time_limit_seconds: int) -> SolveResult:
    assert cp_model is not None

    dates = _month_dates(year, month)
    weekends = _weekends(dates)
    fridays = _fridays(dates)
    thursdays = _thursdays(dates)

    doc_list = list(ALL_DOCTORS)  # excludes TEST_USER by design
    doc_idx = {name: i for i, name in enumerate(doc_list)}
    core_idx = [doc_idx[n] for n in CORE_DOCTORS]
    de_luca_idx = doc_idx["De Luca"]
    saporito_idx = doc_idx["Saporito"]
    carciotto_idx = doc_idx["Carciotto"]
    giusti_idx = doc_idx["Giusti"]

    passes = [{"max_saporito": sp.max_saporito_in_N, "o_balance_tol": sp.o_balance_tolerance} for sp in rules.passes]

    last_fail_log = ""

    for attempt, p in enumerate(passes, start=1):
        model = cp_model.CpModel()

        weekend_var: Dict[Tuple[dt.date, dt.date], cp_model.IntVar] = {}
        for sat, sun in weekends:
            allowed = []
            for idx in core_idx:
                nm = doc_list[idx]
                if _is_available(unavailability, nm, sat) and _is_available(unavailability, nm, sun):
                    allowed.append(idx)
            if not allowed:
                last_fail_log = f"Infeasible: weekend {sat.strftime('%d/%m')}–{sun.strftime('%d/%m')} nessun medico CORE disponibile su entrambi i giorni."
                model = None
                break
            weekend_var[(sat, sun)] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"W_{sat.isoformat()}")

        if model is None:
            continue

        friday_var: Dict[dt.date, cp_model.IntVar] = {}
        for fri in fridays:
            allowed = []
            for idx in core_idx:
                nm = doc_list[idx]
                if _is_available(unavailability, nm, fri):
                    allowed.append(idx)
            if not allowed:
                last_fail_log = f"Infeasible: venerdì {fri.strftime('%d/%m/%Y')} nessun medico CORE disponibile."
                model = None
                break
            friday_var[fri] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"F_{fri.isoformat()}")

        if model is None:
            continue

        j_var: Dict[dt.date, cp_model.IntVar] = {}
        for thu in thursdays:
            allowed = []
            if _is_available(unavailability, "Carciotto", thu):
                allowed.append(carciotto_idx)
            if _is_available(unavailability, "Giusti", thu):
                allowed.append(giusti_idx)
            j_var[thu] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"J_{thu.isoformat()}")

        m_var: Dict[dt.date, Optional[cp_model.IntVar]] = {}
        n_var: Dict[dt.date, cp_model.IntVar] = {}
        o_var: Dict[dt.date, cp_model.IntVar] = {}
        p_var: Dict[dt.date, Optional[cp_model.IntVar]] = {}

        weekend_lookup = {sat: (sat, sun) for (sat, sun) in weekends}
        weekend_lookup.update({sun: (sat, sun) for (sat, sun) in weekends})

        def allowed_for_slot(d: dt.date, col: str) -> List[int]:
            wd = d.weekday()
            allowed = set(range(len(doc_list)))

            # remove unavailable on that day
            for nm in doc_list:
                if not _is_available(unavailability, nm, d):
                    allowed.discard(doc_idx[nm])

            if col == "O":
                allowed.discard(saporito_idx)
                if wd not in (MON, TUE):
                    allowed.discard(de_luca_idx)
                else:
                    any_core = any(_is_available(unavailability, nm, d) for nm in CORE_DOCTORS)
                    if any_core:
                        allowed.discard(de_luca_idx)

            if col == "M":
                if wd != WED:
                    allowed.discard(de_luca_idx)
                allowed.discard(saporito_idx)

            if col == "N":
                if wd not in (MON, TUE, WED):
                    allowed.discard(de_luca_idx)
                if wd in (SAT, SUN):
                    allowed.discard(saporito_idx)

            if col == "P":
                allowed.discard(saporito_idx)

            return sorted(allowed)

        for d in dates:
            wd = d.weekday()

            if wd == TUE:
                m_var[d] = None
            elif d in weekend_lookup:
                m_var[d] = weekend_var[weekend_lookup[d]]
            elif wd == FRI:
                m_var[d] = friday_var[d]
            else:
                allowed = allowed_for_slot(d, "M")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per M il {d.strftime('%d/%m/%Y')}"
                    model = None
                    break
                m_var[d] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"M_{d.isoformat()}")

            if d in weekend_lookup:
                n_var[d] = weekend_var[weekend_lookup[d]]
            else:
                allowed = allowed_for_slot(d, "N")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per N il {d.strftime('%d/%m/%Y')}"
                    model = None
                    break
                n_var[d] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"N_{d.isoformat()}")

            if d in weekend_lookup:
                o_var[d] = weekend_var[weekend_lookup[d]]
            elif wd == FRI:
                o_var[d] = friday_var[d]
            else:
                allowed = allowed_for_slot(d, "O")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per O il {d.strftime('%d/%m/%Y')}"
                    model = None
                    break
                o_var[d] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"O_{d.isoformat()}")

            if wd == WED:
                allowed = allowed_for_slot(d, "P")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per P (mercoledì) il {d.strftime('%d/%m/%Y')}"
                    model = None
                    break
                p_var[d] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"P_{d.isoformat()}")
            else:
                p_var[d] = None

        if model is None:
            continue

        # Friday != immediate weekend
        for fri in fridays:
            w = _first_following_weekend_for_friday(fri, weekends)
            if w is not None:
                model.Add(friday_var[fri] != weekend_var[w])

        # J restrictions (Thu and Fri)
        for thu in thursdays:
            j = j_var[thu]
            if m_var[thu] is not None:
                model.Add(m_var[thu] != j)
            model.Add(n_var[thu] != j)
            model.Add(o_var[thu] != j)

            fri = thu + dt.timedelta(days=1)
            if fri.year == year and fri.month == month and fri.weekday() == FRI:
                model.Add(friday_var[fri] != j)
                model.Add(n_var[fri] != j)

        def add_balancing(vars_list: List[cp_model.IntVar], allowed_doctors: List[int], tol: int, tag: str):
            if not vars_list:
                return {}, None, None
            counts = {}
            max_count = model.NewIntVar(0, len(vars_list), f"{tag}_max")
            min_count = model.NewIntVar(0, len(vars_list), f"{tag}_min")
            for idx in allowed_doctors:
                c = model.NewIntVar(0, len(vars_list), f"{tag}_count_{idx}")
                bools = []
                for k, v in enumerate(vars_list):
                    b = model.NewBoolVar(f"{tag}_is_{idx}_{k}")
                    model.Add(v == idx).OnlyEnforceIf(b)
                    model.Add(v != idx).OnlyEnforceIf(b.Not())
                    bools.append(b)
                model.Add(c == sum(bools))
                counts[idx] = c
                model.Add(max_count >= c)
                model.Add(min_count <= c)
            model.Add(max_count - min_count <= tol)
            return counts, max_count, min_count

        wk_counts, _, _ = add_balancing(list(weekend_var.values()), core_idx, tol=1, tag="wknd")
        fr_counts, _, _ = add_balancing(list(friday_var.values()), core_idx, tol=1, tag="fri")
        j_counts, _, _ = add_balancing(list(j_var.values()), [carciotto_idx, giusti_idx], tol=1, tag="thuJ")

        o_vars_per_day = [o_var[d] for d in dates]
        o_counts, _, _ = add_balancing(o_vars_per_day, core_idx, tol=int(p["o_balance_tol"]), tag="Odist")

        sap_bools = []
        for d in dates:
            b = model.NewBoolVar(f"N_is_sap_{d.isoformat()}")
            model.Add(n_var[d] == saporito_idx).OnlyEnforceIf(b)
            model.Add(n_var[d] != saporito_idx).OnlyEnforceIf(b.Not())
            sap_bools.append(b)
        model.Add(sum(sap_bools) <= int(p["max_saporito"]))

        de_bools = []
        for d in dates:
            for v in (m_var[d], n_var[d], o_var[d], p_var[d]):
                if v is None:
                    continue
                b = model.NewBoolVar(f"is_deluca_{v.Name()}_{d.isoformat()}")
                model.Add(v == de_luca_idx).OnlyEnforceIf(b)
                model.Add(v != de_luca_idx).OnlyEnforceIf(b.Not())
                de_bools.append(b)

        model.Minimize(int(rules.weight_deluca) * sum(de_bools) + int(rules.weight_saporito) * sum(sap_bools))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(time_limit_seconds)
        solver.parameters.num_search_workers = 8

        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            last_fail_log = f"Attempt {attempt}: infeasible/timeout con max_saporito={p['max_saporito']} e tolleranza O={p['o_balance_tol']}."
            continue

        out: Dict[dt.date, Dict[str, str]] = {}
        for d in dates:
            wd = d.weekday()
            row: Dict[str, str] = {}
            if wd == THU:
                row["J"] = doc_list[int(solver.Value(j_var[d]))]
            else:
                row["J"] = ""
            if wd == TUE:
                row["M"] = rules.tue_m_pair
            else:
                v = m_var[d]
                row["M"] = doc_list[int(solver.Value(v))] if v is not None else ""
            row["N"] = doc_list[int(solver.Value(n_var[d]))]
            row["O"] = doc_list[int(solver.Value(o_var[d]))]
            row["P"] = doc_list[int(solver.Value(p_var[d]))] if (wd == WED and p_var[d] is not None) else ""
            out[d] = row

        def _count(name: str, col: str) -> int:
            return sum(1 for d in dates if out[d][col] == name)

        lines = []
        lines.append(f"OK (attempt {attempt}) – {calendar.month_name[month]} {year}")
        lines.append("\nWeekend assignments (by weekend):")
        for nm in CORE_DOCTORS:
            idx = doc_idx[nm]
            if wk_counts:
                lines.append(f"- {nm}: {int(solver.Value(wk_counts[idx]))}")
        lines.append("\nFriday M/O assignments (by Friday):")
        for nm in CORE_DOCTORS:
            idx = doc_idx[nm]
            if fr_counts:
                lines.append(f"- {nm}: {int(solver.Value(fr_counts[idx]))}")
        lines.append("\nThursday J assignments:")
        lines.append(f"- Carciotto: {int(solver.Value(j_counts[carciotto_idx])) if j_counts else 0}")
        lines.append(f"- Giusti: {int(solver.Value(j_counts[giusti_idx])) if j_counts else 0}")
        lines.append("\nO distribution (by day count):")
        for nm in CORE_DOCTORS:
            idx = doc_idx[nm]
            if o_counts:
                lines.append(f"- {nm}: {int(solver.Value(o_counts[idx]))}")
        lines.append(f"\nSaporito in N: {_count('Saporito','N')}")
        lines.append(f"De Luca total appearances (M/N/O/P): {sum(1 for d in dates for c in ['M','N','O','P'] if out[d][c]=='De Luca')}")
        return SolveResult(ok=True, assignment=out, log="\n".join(lines))

    return SolveResult(ok=False, assignment={}, log=last_fail_log or "Infeasible.")


# --------------------------------------------------------------------------------------
# Greedy fallback (only if ortools is not available in the runtime)
# --------------------------------------------------------------------------------------

def _solve_month_greedy(year: int, month: int, unavailability: Dict[str, Set[dt.date]], rules: Rules) -> SolveResult:
    dates = _month_dates(year, month)
    weekends = _weekends(dates)
    fridays = _fridays(dates)
    thursdays = _thursdays(dates)

    # Assign weekends balanced
    wk_assign: Dict[Tuple[dt.date, dt.date], str] = {}
    wk_count = {d: 0 for d in CORE_DOCTORS}

    for w in weekends:
        sat, sun = w
        choices = [nm for nm in CORE_DOCTORS if _is_available(unavailability, nm, sat) and _is_available(unavailability, nm, sun)]
        if not choices:
            return SolveResult(False, {}, f"Infeasible (greedy): weekend {sat.strftime('%d/%m')}–{sun.strftime('%d/%m')} nessun CORE disponibile.")
        # pick min count
        choices.sort(key=lambda nm: wk_count[nm])
        chosen = choices[0]
        wk_assign[w] = chosen
        wk_count[chosen] += 1

    # Assign J on Thursdays balanced between Carciotto/Giusti
    j_assign: Dict[dt.date, str] = {}
    j_count = {"Carciotto": 0, "Giusti": 0}
    for thu in thursdays:
        choices = []
        if _is_available(unavailability, "Carciotto", thu):
            choices.append("Carciotto")
        if _is_available(unavailability, "Giusti", thu):
            choices.append("Giusti")
        if not choices:
            return SolveResult(False, {}, f"Infeasible (greedy): giovedì {thu.strftime('%d/%m/%Y')} J richiede Carciotto o Giusti.")
        choices.sort(key=lambda nm: j_count[nm])
        chosen = choices[0]
        j_assign[thu] = chosen
        j_count[chosen] += 1

    # Assign Fridays balanced, avoiding the immediately following weekend doctor
    fri_assign: Dict[dt.date, str] = {}
    fri_count = {d: 0 for d in CORE_DOCTORS}
    for fri in fridays:
        next_w = _first_following_weekend_for_friday(fri, weekends)
        banned = wk_assign.get(next_w) if next_w else None
        choices = [nm for nm in CORE_DOCTORS if _is_available(unavailability, nm, fri) and nm != banned]
        if not choices:
            # if forced, allow banned (better than fail), but log it
            choices = [nm for nm in CORE_DOCTORS if _is_available(unavailability, nm, fri)]
        if not choices:
            return SolveResult(False, {}, f"Infeasible (greedy): venerdì {fri.strftime('%d/%m/%Y')} nessun CORE disponibile.")
        choices.sort(key=lambda nm: fri_count[nm])
        chosen = choices[0]
        fri_assign[fri] = chosen
        fri_count[chosen] += 1

    # Build daily assignments
    out: Dict[dt.date, Dict[str, str]] = {}
    o_count = {d: 0 for d in CORE_DOCTORS}
    sap_count = 0
    deluca_count = 0

    def pick_allowed(d: dt.date, col: str, banned: Set[str] = set(), prefer_core_balance: bool = False) -> str:
        wd = d.weekday()
        allowed = set(ALL_DOCTORS)
        # remove unavailable
        for nm in list(allowed):
            if not _is_available(unavailability, nm, d):
                allowed.discard(nm)
        allowed -= set(banned)

        if col == "O":
            allowed.discard("Saporito")
            # De Luca only Mon/Tue fallback
            if wd not in (MON, TUE):
                allowed.discard("De Luca")
            else:
                any_core = any(_is_available(unavailability, nm, d) for nm in CORE_DOCTORS)
                if any_core:
                    allowed.discard("De Luca")

        if col == "M":
            allowed.discard("Saporito")
            if wd != WED:
                allowed.discard("De Luca")

        if col == "N":
            if wd not in (MON, TUE, WED):
                allowed.discard("De Luca")
            if wd in (SAT, SUN):
                allowed.discard("Saporito")

        if col == "P":
            allowed.discard("Saporito")

        if not allowed:
            raise ValueError(f"No allowed doctors for {col} on {d}")

        # Prefer CORE balancing for O
        if prefer_core_balance:
            core_allowed = [nm for nm in CORE_DOCTORS if nm in allowed]
            if core_allowed:
                core_allowed.sort(key=lambda nm: o_count[nm])
                return core_allowed[0]

        # Penalize Saporito
        if "Saporito" in allowed:
            # keep it last
            others = [nm for nm in allowed if nm != "Saporito"]
            if others:
                return sorted(others)[0]
        return sorted(allowed)[0]

    weekend_lookup = {sat: (sat, sun) for (sat, sun) in weekends}
    weekend_lookup.update({sun: (sat, sun) for (sat, sun) in weekends})

    for d in dates:
        wd = d.weekday()
        row = {"J": "", "M": "", "N": "", "O": "", "P": ""}

        if wd == THU:
            row["J"] = j_assign[d]
            banned_thu = {row["J"]}
        else:
            banned_thu = set()

        # ban for Friday (day after Thu with J)
        banned_for_day = set()
        if wd == FRI:
            thu = d - dt.timedelta(days=1)
            if thu in j_assign:
                banned_for_day.add(j_assign[thu])

        # M
        if wd == TUE:
            row["M"] = rules.tue_m_pair
        elif d in weekend_lookup:
            row["M"] = wk_assign[weekend_lookup[d]]
        elif wd == FRI:
            row["M"] = fri_assign[d]
        else:
            row["M"] = pick_allowed(d, "M", banned=banned_thu | banned_for_day)

        # N
        if d in weekend_lookup:
            row["N"] = wk_assign[weekend_lookup[d]]
        else:
            row["N"] = pick_allowed(d, "N", banned=banned_thu | banned_for_day)

        # O
        if d in weekend_lookup:
            row["O"] = wk_assign[weekend_lookup[d]]
        elif wd == FRI:
            row["O"] = fri_assign[d]
        else:
            row["O"] = pick_allowed(d, "O", banned=banned_thu | banned_for_day, prefer_core_balance=True)

        # P
        if wd == WED:
            row["P"] = pick_allowed(d, "P")

        out[d] = row

        if row["O"] in CORE_DOCTORS:
            o_count[row["O"]] += 1
        if row["N"] == "Saporito":
            sap_count += 1
        if "De Luca" in (row["M"], row["N"], row["O"], row["P"]):
            deluca_count += 1

    log = [
        "OK (greedy fallback) – ortools non disponibile nel runtime.",
        "Weekend counts: " + ", ".join(f"{k}={v}" for k, v in wk_count.items()),
        "Friday counts: " + ", ".join(f"{k}={v}" for k, v in fri_count.items()),
        "J counts: " + ", ".join(f"{k}={v}" for k, v in j_count.items()),
        "O counts: " + ", ".join(f"{k}={v}" for k, v in o_count.items()),
        f"Saporito in N: {sap_count}",
        f"De Luca appearances: {deluca_count}",
    ]
    return SolveResult(True, out, "\n".join(log))
