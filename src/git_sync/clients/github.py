"""GitHub REST API client.

Wraps just the endpoints git-sync needs:
  - ``list_repos()``  — enumerate repos owned by the authenticated user
  - ``create_repo()`` — create a new repo with visibility + description
  - ``update_repo()`` — patch visibility, description, or default_branch
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .. import VERSION, log
from .http import HTTPClient, HTTPError

_logger = log.get("git_sync.github")

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class GitHubRepo:
    name: str
    full_name: str
    private: bool
    description: str
    default_branch: str
    archived: bool


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = _API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self._http = HTTPClient(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": f"git-sync/{VERSION}",
            },
            timeout=timeout,
        )

    def list_repos(self) -> Iterator[GitHubRepo]:
        for item in self._http.paginate_link(
            "/user/repos",
            params={"affiliation": "owner", "sort": "full_name"},
        ):
            yield _repo_from_dict(item)

    def create_repo(
        self,
        name: str,
        *,
        private: bool,
        description: str = "",
    ) -> GitHubRepo:
        body, _ = self._http.post(
            "/user/repos",
            json_body={
                "name": name,
                "private": private,
                "description": description,
            },
        )
        return _repo_from_dict(body)

    def get_repo(self, owner: str, repo: str) -> GitHubRepo | None:
        try:
            data, _ = self._http.get(f"/repos/{owner}/{repo}")
        except HTTPError as e:
            if e.status == 404:
                return None
            raise
        return _repo_from_dict(data)

    def get_file(
        self, owner: str, repo: str, path: str, *, ref: str | None = None,
    ) -> dict[str, str] | None:
        params = {"ref": ref} if ref else None
        try:
            data, _ = self._http.get(
                f"/repos/{owner}/{repo}/contents/{path}", params=params,
            )
        except HTTPError as e:
            if e.status == 404:
                return None
            raise
        return {
            "content": str(data.get("content") or ""),
            "sha": str(data.get("sha") or ""),
        }

    def put_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content_base64: str,
        *,
        commit_message: str,
        branch: str,
        author_name: str,
        author_email: str,
        sha: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "message": commit_message,
            "content": content_base64,
            "branch": branch,
            "committer": {"name": author_name, "email": author_email},
            "author": {"name": author_name, "email": author_email},
        }
        if sha is not None:
            body["sha"] = sha
        self._http.put(
            f"/repos/{owner}/{repo}/contents/{path}", json_body=body,
        )

    def update_repo(
        self,
        owner: str,
        name: str,
        *,
        private: bool | None = None,
        description: str | None = None,
        default_branch: str | None = None,
        archived: bool | None = None,
    ) -> GitHubRepo:
        patch_body: dict[str, Any] = {}
        if private is not None:
            patch_body["private"] = private
        if description is not None:
            patch_body["description"] = description
        if default_branch is not None:
            patch_body["default_branch"] = default_branch
        if archived is not None:
            patch_body["archived"] = archived
        if not patch_body:
            raise ValueError("update_repo called with no fields to change")

        body, _ = self._http.patch(
            f"/repos/{owner}/{name}",
            json_body=patch_body,
        )
        return _repo_from_dict(body)


def _repo_from_dict(d: dict[str, Any]) -> GitHubRepo:
    return GitHubRepo(
        name=str(d["name"]),
        full_name=str(d["full_name"]),
        private=bool(d["private"]),
        description=str(d.get("description") or ""),
        default_branch=str(d.get("default_branch") or ""),
        archived=bool(d.get("archived", False)),
    )
