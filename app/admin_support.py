from __future__ import annotations

import datetime as dt
from typing import Dict, Optional, Tuple

from .storage_github import GitHubTarget, get_json, put_json, get_text, put_text


DEFAULT_SETTINGS = {
    "unavailability_open": True,
    # 0 = blocco totale (non utile), 31 = di fatto nessun limite.
    "max_unavailability_per_shift": 31,
    "updated_at": "",
    "updated_by": "",
}


def _utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def load_app_settings(token: str, repo: str, branch: str, settings_path: str) -> Tuple[Dict, Optional[str]]:
    tgt = GitHubTarget(repo=repo, branch=branch, path=settings_path)
    data, sha = get_json(token, tgt)
    if not isinstance(data, dict):
        data = {}
    out = dict(DEFAULT_SETTINGS)
    out.update({k: data.get(k) for k in DEFAULT_SETTINGS.keys() if k in data})
    # coerce
    out["unavailability_open"] = bool(out.get("unavailability_open", True))
    try:
        out["max_unavailability_per_shift"] = int(out.get("max_unavailability_per_shift", 31))
    except Exception:
        out["max_unavailability_per_shift"] = 31
    return out, sha


def save_app_settings(
    token: str,
    repo: str,
    branch: str,
    settings_path: str,
    settings: Dict,
    sha: Optional[str],
    updated_by: str = "admin",
) -> str:
    tgt = GitHubTarget(repo=repo, branch=branch, path=settings_path)
    payload = dict(DEFAULT_SETTINGS)
    payload.update({
        "unavailability_open": bool(settings.get("unavailability_open", True)),
        "max_unavailability_per_shift": int(settings.get("max_unavailability_per_shift", 31)),
        "updated_at": _utc_now_iso(),
        "updated_by": str(updated_by or "admin"),
    })
    return put_json(token, tgt, payload, sha, message=f"Update app settings: open={payload['unavailability_open']} max={payload['max_unavailability_per_shift']}")


def audit_path_for_month(audit_dir: str, year: int, month: int) -> str:
    mk = f"{int(year)}-{int(month):02d}"
    base = (audit_dir or "data/audit").strip().strip("/")
    return f"{base}/unavailability_audit_{mk}.csv"


def append_audit_csv(
    token: str,
    repo: str,
    branch: str,
    audit_path: str,
    line: str,
) -> None:
    """Append one CSV line (with trailing \n) to a file stored via GitHub Contents API."""
    tgt = GitHubTarget(repo=repo, branch=branch, path=audit_path)
    cur, sha = get_text(token, tgt)
    # ensure header
    if not cur.strip():
        header = "ts_utc,doctor,year_month,counts\n"
        cur = header
    if not cur.endswith("\n"):
        cur += "\n"
    cur += line
    if not cur.endswith("\n"):
        cur += "\n"
    put_text(token, tgt, cur, sha, message=f"Append audit line ({audit_path})")


def load_audit_csv(
    token: str,
    repo: str,
    branch: str,
    audit_path: str,
) -> str:
    tgt = GitHubTarget(repo=repo, branch=branch, path=audit_path)
    txt, _sha = get_text(token, tgt)
    return txt or ""
