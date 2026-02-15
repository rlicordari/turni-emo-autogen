
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .config import (
    ALL_DOCTORS,
    CORE_DOCTORS,
    MON, TUE, WED, THU, FRI, SAT, SUN,
)
from .rules import load_rules, Rules
from .unavailability import (
    UnavailabilityEntry,
    build_index,
    is_available_for_slot,
    SLOT_MORNING,
    SLOT_AFTERNOON,
    SLOT_NIGHT,
    SLOT_DAY,
)

try:
    from ortools.sat.python import cp_model  # type: ignore
    _HAS_ORTOOLS = True
except Exception:
    cp_model = None  # type: ignore
    _HAS_ORTOOLS = False


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


def _available(index, name: str, d: dt.date, slot: str) -> bool:
    return is_available_for_slot(index, name, d, slot)


def _precheck(year: int, month: int, index) -> Optional[str]:
    dates = _month_dates(year, month)

    # Martedì: M richiede sempre Vizzari + Virga (fascia mattina)
    for d in dates:
        if d.weekday() == TUE:
            if (not _available(index, "Vizzari", d, SLOT_MORNING)) or (not _available(index, "Virga", d, SLOT_MORNING)):
                return (
                    f"Infeasible: il martedì {d.strftime('%d/%m/%Y')} richiede sempre Vizzari+Virga in M "
                    f"(Emodinamica Mattina), ma uno dei due risulta indisponibile in fascia mattina/diurno/tutto il giorno."
                )

    # Giovedì: J richiede Carciotto o Giusti (assunto come attività diurna)
    for d in _thursdays(dates):
        ok_c = _available(index, "Carciotto", d, SLOT_DAY)
        ok_g = _available(index, "Giusti", d, SLOT_DAY)
        if not (ok_c or ok_g):
            return (
                f"Infeasible: il giovedì {d.strftime('%d/%m/%Y')} la colonna J richiede Carciotto o Giusti, "
                f"ma risultano entrambi indisponibili (mattina/pomeriggio/diurno/tutto il giorno)."
            )
    return None


def solve_month(
    year: int,
    month: int,
    unavailability: Dict[str, List[UnavailabilityEntry]],
    time_limit_seconds: int = 10,
) -> SolveResult:
    """Entry point. Uses OR-Tools CP-SAT if available; otherwise a greedy fallback."""
    rules = load_rules(Path(__file__).resolve().parents[1] / "Regole_Emodinamica.yml")
    index = build_index(unavailability)

    msg = _precheck(year, month, index)
    if msg:
        return SolveResult(False, {}, msg)

    if _HAS_ORTOOLS:
        return _solve_month_cpsat(year, month, index, rules=rules, time_limit_seconds=time_limit_seconds)

    return _solve_month_greedy(year, month, index, rules=rules)


# --------------------------------------------------------------------------------------
# CP-SAT solver (preferred)
# --------------------------------------------------------------------------------------

def _solve_month_cpsat(year: int, month: int, index, rules: Rules, time_limit_seconds: int) -> SolveResult:
    assert cp_model is not None

    dates = _month_dates(year, month)
    weekends = _weekends(dates)
    fridays = _fridays(dates)
    thursdays = _thursdays(dates)

    doc_list = list(ALL_DOCTORS)  # excludes TEST_USER by design
    doc_idx = {name: i for i, name in enumerate(doc_list)}
    core_idx = [doc_idx[nm] for nm in CORE_DOCTORS]

    de_luca_idx = doc_idx["De Luca"]
    saporito_idx = doc_idx["Saporito"]
    carciotto_idx = doc_idx["Carciotto"]
    giusti_idx = doc_idx["Giusti"]

    passes = [{"max_saporito": sp.max_saporito_in_N, "o_balance_tol": sp.o_balance_tolerance} for sp in rules.passes]
    last_fail_log = ""

    def _available_slots(name: str, day: dt.date, slots: List[str]) -> bool:
        return all(_available(index, name, day, s) for s in slots)

    for attempt, p in enumerate(passes, start=1):
        model = cp_model.CpModel()

        # Weekend variable: one CORE doctor, same for Sat/Sun and for M,N,O
        weekend_var: Dict[Tuple[dt.date, dt.date], cp_model.IntVar] = {}
        for sat, sun in weekends:
            allowed = []
            for idx in core_idx:
                nm = doc_list[idx]
                # weekend doctor must cover M (morning), N (afternoon), O (night) on both days
                if (
                    _available_slots(nm, sat, [SLOT_MORNING, SLOT_AFTERNOON, SLOT_NIGHT])
                    and _available_slots(nm, sun, [SLOT_MORNING, SLOT_AFTERNOON, SLOT_NIGHT])
                ):
                    allowed.append(idx)
            if not allowed:
                last_fail_log = (
                    f"Infeasible: weekend {sat.strftime('%d/%m')}–{sun.strftime('%d/%m')} "
                    f"nessun medico CORE disponibile su tutte le fasce richieste (M,N,O) in entrambi i giorni."
                )
                model = None
                break
            weekend_var[(sat, sun)] = model.NewIntVarFromDomain(
                cp_model.Domain.FromValues(allowed), f"W_{sat.isoformat()}"
            )

        if model is None:
            continue

        # Friday variable: CORE doctor for M and O (morning + night) on that Friday
        friday_var: Dict[dt.date, cp_model.IntVar] = {}
        for fri in fridays:
            allowed = []
            for idx in core_idx:
                nm = doc_list[idx]
                if _available_slots(nm, fri, [SLOT_MORNING, SLOT_NIGHT]):
                    allowed.append(idx)
            if not allowed:
                last_fail_log = f"Infeasible: venerdì {fri.strftime('%d/%m/%Y')} nessun medico CORE disponibile per M+O (mattina+notte)."
                model = None
                break
            friday_var[fri] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"F_{fri.isoformat()}")

        if model is None:
            continue

        # J on Thursdays: Carciotto or Giusti (diurno)
        j_var: Dict[dt.date, cp_model.IntVar] = {}
        for thu in thursdays:
            allowed = []
            if _available(index, "Carciotto", thu, SLOT_DAY):
                allowed.append(carciotto_idx)
            if _available(index, "Giusti", thu, SLOT_DAY):
                allowed.append(giusti_idx)
            if not allowed:
                last_fail_log = f"Infeasible: giovedì {thu.strftime('%d/%m/%Y')} nessun candidato disponibile per J."
                model = None
                break
            j_var[thu] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"J_{thu.isoformat()}")

        if model is None:
            continue

        m_var: Dict[dt.date, Optional[cp_model.IntVar]] = {}
        n_var: Dict[dt.date, cp_model.IntVar] = {}
        o_var: Dict[dt.date, cp_model.IntVar] = {}
        p_var: Dict[dt.date, Optional[cp_model.IntVar]] = {}

        weekend_lookup = {sat: (sat, sun) for (sat, sun) in weekends}
        weekend_lookup.update({sun: (sat, sun) for (sat, sun) in weekends})

        def allowed_for_slot(day: dt.date, col: str) -> List[int]:
            """Allowed doctor indexes for a specific column on a specific day, considering fascia-based unavailability."""
            wd = day.weekday()
            allowed = set(range(len(doc_list)))

            # 1) availability by slot
            if col in ("M", "P"):
                slot = SLOT_MORNING
            elif col == "N":
                slot = SLOT_AFTERNOON
            elif col == "O":
                slot = SLOT_NIGHT
            else:
                slot = SLOT_DAY

            for nm in doc_list:
                if not _available(index, nm, day, slot):
                    allowed.discard(doc_idx[nm])

            # 2) hard pool rules
            if col == "O":
                allowed.discard(saporito_idx)  # never Saporito in O

                # De Luca can appear in O on Mon/Tue/Wed, but:
                # - Mon/Tue only if ALL core are unavailable for NIGHT that day (fallback)
                # - Wed: allowed normally
                if wd not in (MON, TUE, WED):
                    allowed.discard(de_luca_idx)
                elif wd in (MON, TUE):
                    any_core_available = any(_available(index, nm, day, SLOT_NIGHT) for nm in CORE_DOCTORS)
                    if any_core_available:
                        allowed.discard(de_luca_idx)

            if col == "M":
                allowed.discard(saporito_idx)
                if wd != WED:
                    allowed.discard(de_luca_idx)

            if col == "N":
                # De Luca only Mon/Tue/Wed
                if wd not in (MON, TUE, WED):
                    allowed.discard(de_luca_idx)
                # Saporito only Mon-Fri
                if wd in (SAT, SUN):
                    allowed.discard(saporito_idx)

            if col == "P":
                # only Wed, handled outside; but keep safe
                allowed.discard(saporito_idx)
                if wd != WED:
                    allowed.discard(de_luca_idx)

            return sorted(allowed)

        # Build per-day variables with special rules (Tue pair, weekend, Friday)
        for day in dates:
            wd = day.weekday()

            # M
            if wd == TUE:
                m_var[day] = None  # fixed string (Vizzari/Virga)
            elif day in weekend_lookup:
                m_var[day] = weekend_var[weekend_lookup[day]]
            elif wd == FRI:
                m_var[day] = friday_var[day]
            else:
                allowed = allowed_for_slot(day, "M")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per M (mattina) il {day.strftime('%d/%m/%Y')}"
                    model = None
                    break
                m_var[day] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"M_{day.isoformat()}")

            # N
            if day in weekend_lookup:
                n_var[day] = weekend_var[weekend_lookup[day]]
            else:
                allowed = allowed_for_slot(day, "N")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per N (pomeriggio) il {day.strftime('%d/%m/%Y')}"
                    model = None
                    break
                n_var[day] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"N_{day.isoformat()}")

            # O
            if day in weekend_lookup:
                o_var[day] = weekend_var[weekend_lookup[day]]
            elif wd == FRI:
                o_var[day] = friday_var[day]
            else:
                allowed = allowed_for_slot(day, "O")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per O (notte) il {day.strftime('%d/%m/%Y')}"
                    model = None
                    break
                o_var[day] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"O_{day.isoformat()}")

            # P (only Wed)
            if wd == WED:
                allowed = allowed_for_slot(day, "P")
                if not allowed:
                    last_fail_log = f"Infeasible: nessun medico disponibile per P (ambulatorio mattina) il {day.strftime('%d/%m/%Y')}"
                    model = None
                    break
                p_var[day] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed), f"P_{day.isoformat()}")
            else:
                p_var[day] = None

        if model is None:
            continue

        # Friday doctor != immediate following weekend doctor
        for fri in fridays:
            w = _first_following_weekend_for_friday(fri, weekends)
            if w is not None:
                model.Add(friday_var[fri] != weekend_var[w])

        # J restrictions (Thu and Fri): the J doctor cannot appear in M/N/O on Thu nor the day after
        for thu in thursdays:
            j = j_var[thu]
            if m_var[thu] is not None:
                model.Add(m_var[thu] != j)
            model.Add(n_var[thu] != j)
            model.Add(o_var[thu] != j)

            nxt = thu + dt.timedelta(days=1)
            if nxt.year == year and nxt.month == month and nxt.weekday() == FRI:
                model.Add(friday_var[nxt] != j)  # covers M and O on Friday
                model.Add(n_var[nxt] != j)

        # --- Balancing helpers ---
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

        # O distribution balanced among CORE (De Luca only Mon/Tue fallback, but on Wed could appear; keep as soft)
        o_vars_per_day = [o_var[d] for d in dates]
        o_counts, _, _ = add_balancing(o_vars_per_day, core_idx, tol=int(p["o_balance_tol"]), tag="Odist")

        # Saporito in N limited (soft via multipass)
        sap_bools = []
        for day in dates:
            b = model.NewBoolVar(f"N_is_sap_{day.isoformat()}")
            model.Add(n_var[day] == saporito_idx).OnlyEnforceIf(b)
            model.Add(n_var[day] != saporito_idx).OnlyEnforceIf(b.Not())
            sap_bools.append(b)
        model.Add(sum(sap_bools) <= int(p["max_saporito"]))

        # Penalize De Luca usage (any slot)
        de_bools = []
        for day in dates:
            for v in (m_var[day], n_var[day], o_var[day], p_var[day]):
                if v is None:
                    continue
                b = model.NewBoolVar(f"is_deluca_{v.Name()}_{day.isoformat()}")
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
        for day in dates:
            wd = day.weekday()
            row: Dict[str, str] = {}
            row["J"] = doc_list[int(solver.Value(j_var[day]))] if wd == THU else ""
            row["M"] = rules.tue_m_pair if wd == TUE else doc_list[int(solver.Value(m_var[day]))]  # type: ignore
            row["N"] = doc_list[int(solver.Value(n_var[day]))]
            row["O"] = doc_list[int(solver.Value(o_var[day]))]
            row["P"] = doc_list[int(solver.Value(p_var[day]))] if (wd == WED and p_var[day] is not None) else ""
            out[day] = row

        def _count(name: str, col: str) -> int:
            return sum(1 for day in dates if out[day][col] == name)

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

        lines.append("\nO distribution (by day count, CORE only):")
        for nm in CORE_DOCTORS:
            idx = doc_idx[nm]
            if o_counts:
                lines.append(f"- {nm}: {int(solver.Value(o_counts[idx]))}")

        lines.append(f"\nSaporito in N: {_count('Saporito','N')}")
        lines.append(f"De Luca total appearances (M/N/O/P): {sum(1 for day in dates for c in ['M','N','O','P'] if out[day][c]=='De Luca')}")
        return SolveResult(ok=True, assignment=out, log="\n".join(lines))

    return SolveResult(ok=False, assignment={}, log=last_fail_log or "Infeasible.")


# --------------------------------------------------------------------------------------
# Greedy fallback (only if ortools is not available in the runtime)
# --------------------------------------------------------------------------------------

def _solve_month_greedy(year: int, month: int, index, rules: Rules) -> SolveResult:
    dates = _month_dates(year, month)
    weekends = _weekends(dates)
    fridays = _fridays(dates)
    thursdays = _thursdays(dates)

    def avail(doc: str, day: dt.date, slot: str) -> bool:
        return _available(index, doc, day, slot)

    # Assign weekends balanced (must cover M,N,O)
    wk_assign: Dict[Tuple[dt.date, dt.date], str] = {}
    wk_count = {d: 0 for d in CORE_DOCTORS}
    for sat, sun in weekends:
        def ok(doc: str) -> bool:
            return (
                avail(doc, sat, SLOT_MORNING) and avail(doc, sat, SLOT_AFTERNOON) and avail(doc, sat, SLOT_NIGHT)
                and avail(doc, sun, SLOT_MORNING) and avail(doc, sun, SLOT_AFTERNOON) and avail(doc, sun, SLOT_NIGHT)
            )
        choices = [nm for nm in CORE_DOCTORS if ok(nm)]
        if not choices:
            return SolveResult(False, {}, f"Infeasible (greedy): weekend {sat.strftime('%d/%m')}–{sun.strftime('%d/%m')} nessun CORE disponibile su M/N/O.")
        choices.sort(key=lambda nm: wk_count[nm])
        chosen = choices[0]
        wk_assign[(sat, sun)] = chosen
        wk_count[chosen] += 1

    # Assign J on Thursdays balanced between Carciotto/Giusti (diurno)
    j_assign: Dict[dt.date, str] = {}
    j_count = {"Carciotto": 0, "Giusti": 0}
    for thu in thursdays:
        choices = []
        if avail("Carciotto", thu, SLOT_DAY):
            choices.append("Carciotto")
        if avail("Giusti", thu, SLOT_DAY):
            choices.append("Giusti")
        if not choices:
            return SolveResult(False, {}, f"Infeasible (greedy): giovedì {thu.strftime('%d/%m/%Y')} J richiede Carciotto o Giusti.")
        choices.sort(key=lambda nm: j_count[nm])
        chosen = choices[0]
        j_assign[thu] = chosen
        j_count[chosen] += 1

    # Assign Fridays balanced, avoiding the immediately following weekend doctor (must cover M+O)
    fri_assign: Dict[dt.date, str] = {}
    fri_count = {d: 0 for d in CORE_DOCTORS}
    for fri in fridays:
        next_w = _first_following_weekend_for_friday(fri, weekends)
        banned = wk_assign.get(next_w) if next_w else None
        choices = [nm for nm in CORE_DOCTORS if avail(nm, fri, SLOT_MORNING) and avail(nm, fri, SLOT_NIGHT) and nm != banned]
        if not choices:
            choices = [nm for nm in CORE_DOCTORS if avail(nm, fri, SLOT_MORNING) and avail(nm, fri, SLOT_NIGHT)]
        if not choices:
            return SolveResult(False, {}, f"Infeasible (greedy): venerdì {fri.strftime('%d/%m/%Y')} nessun CORE disponibile per M+O.")
        choices.sort(key=lambda nm: fri_count[nm])
        chosen = choices[0]
        fri_assign[fri] = chosen
        fri_count[chosen] += 1

    weekend_lookup = {sat: (sat, sun) for (sat, sun) in weekends}
    weekend_lookup.update({sun: (sat, sun) for (sat, sun) in weekends})

    out: Dict[dt.date, Dict[str, str]] = {}
    o_count = {d: 0 for d in CORE_DOCTORS}
    sap_count = 0

    def pick_allowed(day: dt.date, col: str, banned: Set[str] = set(), prefer_core_balance: bool = False) -> str:
        wd = day.weekday()

        if col in ("M", "P"):
            slot = SLOT_MORNING
        elif col == "N":
            slot = SLOT_AFTERNOON
        else:
            slot = SLOT_NIGHT

        allowed = set(ALL_DOCTORS)

        # slot-based availability
        allowed = {nm for nm in allowed if avail(nm, day, slot)}
        allowed -= set(banned)

        # pool hard rules
        if col == "O":
            allowed.discard("Saporito")
            if wd not in (MON, TUE, WED):
                allowed.discard("De Luca")
            elif wd in (MON, TUE):
                any_core = any(avail(nm, day, SLOT_NIGHT) for nm in CORE_DOCTORS)
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
            if wd != WED:
                allowed.discard("De Luca")

        if not allowed:
            raise ValueError(f"No allowed doctors for {col} on {day}")

        if prefer_core_balance:
            core_allowed = [nm for nm in CORE_DOCTORS if nm in allowed]
            if core_allowed:
                core_allowed.sort(key=lambda nm: o_count[nm])
                return core_allowed[0]

        # Penalize Saporito: keep it last
        if "Saporito" in allowed and len(allowed) > 1:
            allowed = {nm for nm in allowed if nm != "Saporito"}  # greedy: don't choose unless forced

        return sorted(allowed)[0]

    # Build schedule day-by-day
    for day in dates:
        wd = day.weekday()
        row = {"J": "", "M": "", "N": "", "O": "", "P": ""}

        # J Thu
        if wd == THU:
            row["J"] = j_assign[day]

        # M
        if wd == TUE:
            row["M"] = rules.tue_m_pair
        elif day in weekend_lookup:
            row["M"] = wk_assign[weekend_lookup[day]]
        elif wd == FRI:
            row["M"] = fri_assign[day]
        else:
            banned = set()
            if wd == THU:
                banned.add(row["J"])
            row["M"] = pick_allowed(day, "M", banned=banned)

        # N
        if day in weekend_lookup:
            row["N"] = wk_assign[weekend_lookup[day]]
        else:
            banned = set()
            if wd == THU:
                banned.add(row["J"])
            # next day after Thu (Fri) J cannot appear in N
            if wd == FRI and (day - dt.timedelta(days=1)).weekday() == THU:
                jdoc = j_assign.get(day - dt.timedelta(days=1))
                if jdoc:
                    banned.add(jdoc)
            row["N"] = pick_allowed(day, "N", banned=banned)

        # O
        if day in weekend_lookup:
            row["O"] = wk_assign[weekend_lookup[day]]
        elif wd == FRI:
            row["O"] = fri_assign[day]
        else:
            banned = set()
            if wd == THU:
                banned.add(row["J"])
            row["O"] = pick_allowed(day, "O", banned=banned, prefer_core_balance=True)
            if row["O"] in CORE_DOCTORS:
                o_count[row["O"]] += 1

        # P (Wed)
        if wd == WED:
            row["P"] = pick_allowed(day, "P")

        # enforce J restriction on Fri for M/O already via fri_assign; in greedy we don't add extra here.

        out[day] = row

    lines = ["OK (greedy fallback)"]
    return SolveResult(ok=True, assignment=out, log="\n".join(lines))
