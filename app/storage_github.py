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
    path: str          # e.g. "data/unavailability.json"


class GitHubStoreError(RuntimeError):
    pass


def _headers(token: str) -> Dict[str, str]:
    # NOTE: GitHub accetta anche "Bearer" con fine-grained PAT; "token" resta
    # compatibile con PAT classic. Manteniamo "token" per semplicità.
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def _get_contents(token: str, target: GitHubTarget) -> Tuple[Optional[dict], Optional[str]]:
    url = f"{GITHUB_API}/repos/{target.repo}/contents/{target.path}"
    r = requests.get(url, headers=_headers(token), params={"ref": target.branch}, timeout=30)
    if r.status_code == 404:
        return None, None
    if r.status_code != 200:
        raise GitHubStoreError(f"GitHub GET failed ({r.status_code}): {r.text[:500]}")
    payload = r.json()
    return payload, payload.get("sha")


def get_text(token: str, target: GitHubTarget) -> Tuple[str, Optional[str]]:
    """Read a text file from GitHub Contents API. Returns (text, sha)."""
    payload, sha = _get_contents(token, target)
    if payload is None:
        return "", None
    content_b64 = payload.get("content", "")
    raw = base64.b64decode(content_b64).decode("utf-8") if content_b64 else ""
    return raw, sha


def put_text(token: str, target: GitHubTarget, text: str, sha: Optional[str], message: str) -> str:
    """Write a text file to GitHub Contents API. Returns new sha."""
    url = f"{GITHUB_API}/repos/{target.repo}/contents/{target.path}"
    content_b64 = base64.b64encode((text or "").encode("utf-8")).decode("utf-8")

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


def get_json(token: str, target: GitHubTarget) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read JSON from GitHub Contents API. Returns (data, sha)."""
    raw, sha = get_text(token, target)
    if not raw.strip():
        return {}, sha
    try:
        return json.loads(raw), sha
    except Exception as e:
        raise GitHubStoreError(f"Invalid JSON in {target.path}: {e}") from e


def put_json(token: str, target: GitHubTarget, data: Dict[str, Any], sha: Optional[str], message: str) -> str:
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    return put_text(token=token, target=target, text=raw, sha=sha, message=message)
