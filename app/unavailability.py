from __future__ import annotations

import datetime as dt
from typing import Dict, List, Set, Tuple, Any

from .storage_github import GitHubTarget, get_json, put_json


def _today_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ensure_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("doctors", {})
    data.setdefault("updated_at", _today_iso())
    if not isinstance(data["doctors"], dict):
        data["doctors"] = {}
    return data


def _to_iso(d: dt.date) -> str:
    return d.isoformat()


def _from_iso(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def load_all(token: str, target: GitHubTarget) -> Tuple[Dict[str, Set[dt.date]], str | None]:
    raw, sha = get_json(token, target)
    raw = _ensure_schema(raw)

    out: Dict[str, Set[dt.date]] = {}
    for name, payload in (raw.get("doctors") or {}).items():
        dates = set()
        for s in (payload.get("dates") or []):
            try:
                dates.add(_from_iso(str(s)))
            except Exception:
                continue
        out[str(name)] = dates

    return out, sha


def save_doctor_month(
    token: str,
    target: GitHubTarget,
    doctor: str,
    year: int,
    month: int,
    unavailable_dates_in_month: Set[dt.date],
) -> None:
    """Replace doctor's unavailable dates for the selected month, keeping other months."""
    all_data, sha = get_json(token, target)
    all_data = _ensure_schema(all_data)
    doctors = all_data["doctors"]
    entry = doctors.get(doctor) or {}
    existing = set()
    for s in entry.get("dates") or []:
        try:
            existing.add(_from_iso(str(s)))
        except Exception:
            pass

    # Remove old dates for that month/year
    kept = {d for d in existing if not (d.year == year and d.month == month)}
    merged = kept | set(unavailable_dates_in_month)

    doctors[doctor] = {"dates": sorted([_to_iso(d) for d in merged])}
    all_data["doctors"] = doctors
    all_data["updated_at"] = _today_iso()

    put_json(
        token=token,
        target=target,
        data=all_data,
        sha=sha,
        message=f"Update unavailability: {doctor} {year}-{month:02d}",
    )


def get_doctor_month(all_unavail: Dict[str, Set[dt.date]], doctor: str, year: int, month: int) -> Set[dt.date]:
    dates = set(all_unavail.get(doctor, set()))
    return {d for d in dates if d.year == year and d.month == month}
