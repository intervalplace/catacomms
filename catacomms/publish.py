"""Push a board to a static host, without anybody having to remember to.

The website is https and a home node is not, so a page on catacomms.org cannot
fetch a board from your laptop: browsers refuse mixed content, and no amount
of configuration changes that. A node serves its own board perfectly well over
plain http on your own network, but a public page needs the records pushed to
it rather than pulled from you.

So this puts the file where the site can read it. GitHub's contents API is the
target because a static host watching a repository will redeploy on its own,
which means the whole chain from finishing a delve to the board updating has
nobody in it.

Standard library only: one GET for the current revision, one PUT with the new
one.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/{repo}/contents/{path}"
TOKEN_PATH = Path.home() / ".catacomms" / "github-token"


def read_token(explicit: str = "") -> str:
    """A token from the flag, the environment, or a file. Never a prompt: this
    runs unattended after a delve."""
    if explicit:
        return explicit.strip()
    from_env = os.environ.get("CATACOMMS_GITHUB_TOKEN", "").strip()
    if from_env:
        return from_env
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return ""


def _request(url: str, token: str, method: str = "GET", body=None):
    request = urllib.request.Request(url, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "catacomms")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, data, timeout=20) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def publish(content: str, repo: str, path: str = "board.json",
            token: str = "", branch: str = "", message: str = "") -> str:
    """Write `content` to `path` in `repo`. Returns a short description.

    Raises RuntimeError with something readable rather than a traceback,
    because this is called from a game loop and a failed push is not a reason
    to lose the evening.
    """
    token = read_token(token)
    if not token:
        raise RuntimeError(
            "No GitHub token. Put one in ~/.catacomms/github-token, or set "
            "CATACOMMS_GITHUB_TOKEN. It needs contents write on that "
            "repository and nothing else.")
    if "/" not in repo:
        raise RuntimeError("Repository should look like owner/name.")

    url = API.format(repo=repo, path=path.lstrip("/"))
    if branch:
        url += f"?ref={branch}"

    sha = ""
    try:
        existing = _request(url, token)
        sha = existing.get("sha", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            sha = ""            # first publish
        elif exc.code in (401, 403):
            raise RuntimeError("GitHub refused the token. Check it has "
                               "contents write on that repository.")
        else:
            raise RuntimeError(f"GitHub said {exc.code} looking for {path}.")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GitHub: {exc.reason}")

    body = {
        "message": message or "catacomms: board",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    if branch:
        body["branch"] = branch

    try:
        result = _request(API.format(repo=repo, path=path.lstrip("/")),
                          token, "PUT", body)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"GitHub refused the write ({exc.code}) {detail}".strip())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GitHub: {exc.reason}")

    commit = (result.get("commit") or {}).get("sha", "")[:7]
    return f"published {path} to {repo}" + (f" as {commit}" if commit else "")
