from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class GitHubTarget:
    repo: str          # "OWNER/REPO"
    branch: str        # "main"
    path: str          # "data/unavailability.json"


class GitHubStoreError(RuntimeError):
    pass


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def get_json(token: str, target: GitHubTarget) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read JSON from GitHub Contents API.
    Returns (data, sha). If file doesn't exist, returns ({}, None).
    """
    url = f"{GITHUB_API}/repos/{target.repo}/contents/{target.path}"
    r = requests.get(url, headers=_headers(token), params={"ref": target.branch}, timeout=30)
    if r.status_code == 404:
        return {}, None
    if r.status_code != 200:
        raise GitHubStoreError(f"GitHub GET failed ({r.status_code}): {r.text[:500]}")
    payload = r.json()
    content_b64 = payload.get("content", "")
    sha = payload.get("sha")
    raw = base64.b64decode(content_b64).decode("utf-8") if content_b64 else ""
    if not raw.strip():
        return {}, sha
    try:
        return json.loads(raw), sha
    except Exception as e:
        raise GitHubStoreError(f"Invalid JSON in {target.path}: {e}") from e


def put_json(token: str, target: GitHubTarget, data: Dict[str, Any], sha: Optional[str], message: str) -> str:
    """Write JSON to GitHub Contents API. Returns new sha."""
    url = f"{GITHUB_API}/repos/{target.repo}/contents/{target.path}"
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(raw.encode("utf-8")).decode("utf-8")

    body: Dict[str, Any] = {
        "message": message,
        "content": content_b64,
        "branch": target.branch,
    }
    if sha:
        body["sha"] = sha

    r = requests.put(url, headers=_headers(token), json=body, timeout=30)
    if r.status_code not in (200, 201):
        raise GitHubStoreError(f"GitHub PUT failed ({r.status_code}): {r.text[:500]}")
    return r.json().get("content", {}).get("sha") or ""
