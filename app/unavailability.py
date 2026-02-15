
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .storage_github import GitHubTarget, get_json, put_json

# Allineato al progetto "spunto" (turni_autogen)
VALID_SHIFTS = ["Mattina", "Pomeriggio", "Notte", "Diurno", "Tutto il giorno"]
_VALID_SET = set(VALID_SHIFTS)

# Slot logici usati dal generatore turni
SLOT_MORNING = "MORNING"      # M, P
SLOT_AFTERNOON = "AFTERNOON"  # N
SLOT_NIGHT = "NIGHT"          # O
SLOT_DAY = "DAY"              # J (assunto come attività diurna)

SLOT_BLOCKS: Dict[str, Set[str]] = {
    SLOT_MORNING: {"Mattina", "Diurno", "Tutto il giorno"},
    SLOT_AFTERNOON: {"Pomeriggio", "Diurno", "Tutto il giorno"},
    SLOT_NIGHT: {"Notte", "Tutto il giorno"},
    SLOT_DAY: {"Mattina", "Pomeriggio", "Diurno", "Tutto il giorno"},
}


@dataclass(frozen=True)
class UnavailabilityEntry:
    date: dt.date
    shift: str
    note: str = ""
    updated_at: str = ""


def _utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def norm_shift(s: str) -> str:
    """Normalizza alias e restituisce uno dei VALID_SHIFTS (se riconosciuto)."""
    s0 = (s or "").strip()
    low = s0.lower()

    if low in {"matt", "mattina"}:
        return "Mattina"
    if low in {"pom", "pomeriggio", "pomer"}:
        return "Pomeriggio"
    if low in {"notte", "night"}:
        return "Notte"
    if low.startswith("diurn"):
        return "Diurno"
    if low.startswith("tutto"):
        return "Tutto il giorno"

    # se già valido, lo lasciamo
    if s0 in _VALID_SET:
        return s0
    return s0  # sconosciuto: verrà ignorato lato business rules


def _ensure_schema(data: Any) -> Dict[str, Any]:
    """
    Schema v2 (JSON):
      {
        "version": 2,
        "updated_at": "2026-02-..Z",
        "doctors": {
          "<Doctor>": {
            "entries": [{"date":"YYYY-MM-DD","shift":"Mattina","note":"","updated_at":"...Z"}, ...]
          },
          ...
        }
      }

    Migrazione automatica da v1:
      {"version":1,"doctors":{"<Doctor>":{"dates":["YYYY-MM-DD",...]}}}
      -> entries con shift "Tutto il giorno".
    """
    if not isinstance(data, dict):
        data = {}

    ver = int(data.get("version") or 1)

    doctors = data.get("doctors")
    if not isinstance(doctors, dict):
        doctors = {}

    # migrate v1 -> v2
    if ver <= 1:
        migrated: Dict[str, Any] = {}
        for name, payload in doctors.items():
            if not isinstance(payload, dict):
                payload = {}
            dates = payload.get("dates") or []
            entries = []
            for s in dates:
                try:
                    d = dt.date.fromisoformat(str(s)[:10])
                except Exception:
                    continue
                entries.append({
                    "date": d.isoformat(),
                    "shift": "Tutto il giorno",
                    "note": "",
                    "updated_at": "",
                })
            migrated[str(name)] = {"entries": entries}
        doctors = migrated
        ver = 2

    data["version"] = 2
    data["doctors"] = doctors
    data["updated_at"] = str(data.get("updated_at") or _utc_now_iso())
    return data


def load_all(token: str, target: GitHubTarget) -> Tuple[Dict[str, List[UnavailabilityEntry]], str | None]:
    raw, sha = get_json(token, target)
    raw = _ensure_schema(raw)

    out: Dict[str, List[UnavailabilityEntry]] = {}
    for name, payload in (raw.get("doctors") or {}).items():
        entries: List[UnavailabilityEntry] = []
        if isinstance(payload, dict):
            raw_entries = payload.get("entries") or []
        else:
            raw_entries = []
        for r in raw_entries:
            if not isinstance(r, dict):
                continue
            ds = str(r.get("date") or "")[:10]
            try:
                d = dt.date.fromisoformat(ds)
            except Exception:
                continue
            sh = norm_shift(str(r.get("shift") or ""))
            if sh not in _VALID_SET:
                # ignoriamo fasce non standard
                continue
            note = str(r.get("note") or "")
            upd = str(r.get("updated_at") or "")
            entries.append(UnavailabilityEntry(date=d, shift=sh, note=note, updated_at=upd))
        out[str(name)] = entries

    return out, sha


def get_doctor_month(all_unavail: Dict[str, List[UnavailabilityEntry]], doctor: str, year: int, month: int) -> List[UnavailabilityEntry]:
    rows = list(all_unavail.get(doctor, []) or [])
    return [e for e in rows if (e.date.year == year and e.date.month == month)]


def _replace_doctor_month(
    existing_entries: List[UnavailabilityEntry],
    year: int,
    month: int,
    new_entries_month: List[UnavailabilityEntry],
) -> List[UnavailabilityEntry]:
    kept = [e for e in existing_entries if not (e.date.year == year and e.date.month == month)]
    return kept + new_entries_month


def save_doctor_month(
    token: str,
    target: GitHubTarget,
    doctor: str,
    year: int,
    month: int,
    entries_in_month: List[UnavailabilityEntry],
) -> None:
    """Replace doctor's entries for the selected month, keeping other months."""
    all_data, sha = get_json(token, target)
    all_data = _ensure_schema(all_data)

    doctors = all_data["doctors"]
    entry = doctors.get(doctor)
    if not isinstance(entry, dict):
        entry = {"entries": []}

    existing_raw = entry.get("entries") or []
    existing: List[UnavailabilityEntry] = []
    for r in existing_raw:
        if not isinstance(r, dict):
            continue
        ds = str(r.get("date") or "")[:10]
        try:
            d = dt.date.fromisoformat(ds)
        except Exception:
            continue
        sh = norm_shift(str(r.get("shift") or ""))
        if sh not in _VALID_SET:
            continue
        existing.append(UnavailabilityEntry(date=d, shift=sh, note=str(r.get("note") or ""), updated_at=str(r.get("updated_at") or "")))

    # normalize & stamp
    stamped: List[UnavailabilityEntry] = []
    now = _utc_now_iso()
    for e in entries_in_month:
        sh = norm_shift(e.shift)
        if sh not in _VALID_SET:
            continue
        stamped.append(UnavailabilityEntry(date=e.date, shift=sh, note=str(e.note or ""), updated_at=now))

    merged = _replace_doctor_month(existing, year, month, stamped)

    # serialize (sorted for stability)
    def _key(e: UnavailabilityEntry):
        return (e.date.isoformat(), e.shift, e.note)

    merged_sorted = sorted(merged, key=_key)
    doctors[doctor] = {
        "entries": [
            {"date": e.date.isoformat(), "shift": e.shift, "note": e.note, "updated_at": e.updated_at}
            for e in merged_sorted
        ]
    }
    all_data["doctors"] = doctors
    all_data["updated_at"] = now

    put_json(
        token=token,
        target=target,
        data=all_data,
        sha=sha,
        message=f"Update unavailability: {doctor} {year}-{month:02d}",
    )


def build_index(all_unavail: Dict[str, List[UnavailabilityEntry]]) -> Dict[str, Dict[dt.date, Set[str]]]:
    """doctor -> date -> set(shifts)"""
    idx: Dict[str, Dict[dt.date, Set[str]]] = {}
    for doc, entries in (all_unavail or {}).items():
        dmap: Dict[dt.date, Set[str]] = {}
        for e in entries or []:
            dmap.setdefault(e.date, set()).add(e.shift)
        idx[str(doc)] = dmap
    return idx


def is_available_for_slot(index: Dict[str, Dict[dt.date, Set[str]]], doctor: str, day: dt.date, slot: str) -> bool:
    blocked = SLOT_BLOCKS.get(slot, set())
    shifts = (index.get(doctor) or {}).get(day, set())
    return shifts.isdisjoint(blocked)


def save_doctor_months(
    token: str,
    target: GitHubTarget,
    doctor: str,
    updates: Dict[Tuple[int, int], List[UnavailabilityEntry]],
) -> None:
    """Replace multiple months for one doctor in a single GitHub write."""
    if not updates:
        return

    all_data, sha = get_json(token, target)
    all_data = _ensure_schema(all_data)

    doctors = all_data["doctors"]
    entry = doctors.get(doctor)
    if not isinstance(entry, dict):
        entry = {"entries": []}

    # Parse existing entries
    existing_raw = entry.get("entries") or []
    existing: List[UnavailabilityEntry] = []
    for r in existing_raw:
        if not isinstance(r, dict):
            continue
        ds = str(r.get("date") or "")[:10]
        try:
            d = dt.date.fromisoformat(ds)
        except Exception:
            continue
        sh = norm_shift(str(r.get("shift") or ""))
        if sh not in _VALID_SET:
            continue
        existing.append(UnavailabilityEntry(date=d, shift=sh, note=str(r.get("note") or ""), updated_at=str(r.get("updated_at") or "")))

    now = _utc_now_iso()

    # Apply updates month-by-month
    merged = list(existing)
    for (yy, mm), entries_in_month in updates.items():
        stamped: List[UnavailabilityEntry] = []
        for e in entries_in_month or []:
            sh = norm_shift(e.shift)
            if sh not in _VALID_SET:
                continue
            stamped.append(UnavailabilityEntry(date=e.date, shift=sh, note=str(e.note or ""), updated_at=now))
        merged = _replace_doctor_month(merged, int(yy), int(mm), stamped)

    def _key(e: UnavailabilityEntry):
        return (e.date.isoformat(), e.shift, e.note)

    merged_sorted = sorted(merged, key=_key)

    doctors[doctor] = {
        "entries": [
            {"date": e.date.isoformat(), "shift": e.shift, "note": e.note, "updated_at": e.updated_at}
            for e in merged_sorted
        ]
    }
    all_data["doctors"] = doctors
    all_data["updated_at"] = now

    put_json(
        token=token,
        target=target,
        data=all_data,
        sha=sha,
        message=f"Update unavailability: {doctor} (multi-month) {now}",
    )
