"""GitLab REST API client.

Wraps just the endpoints git-sync needs:
  - ``me()``                for resolving the authenticated user's id
  - ``list_projects()``     for mirror enumeration
  - ``get_languages()``     for per-project language stats
  - ``list_user_events()``  for the profile activity feed
  - ``resolve_project_id()`` / ``get_file()`` / ``put_file()`` for publishing
    profile READMEs via the Repository Files API.
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .. import VERSION, log
from .http import HTTPClient, HTTPError

_logger = log.get("git_sync.gitlab")


@dataclass(frozen=True)
class GitLabUser:
    id: int
    username: str


@dataclass(frozen=True)
class GitLabProject:
    id: int
    path_with_namespace: str
    name: str
    description: str
    visibility: str  # "public" | "internal" | "private"
    default_branch: str | None
    last_activity_at: str  # ISO 8601
    archived: bool
    size_bytes: int  # statistics.repository_size; 0 if unavailable
    namespace_kind: str  # "user" | "group"


@dataclass(frozen=True)
class GitLabEvent:
    id: int
    action_name: str
    created_at: str
    target_type: str | None
    target_title: str | None
    project_id: int | None


# Access level constants from the GitLab API.
# https://docs.gitlab.com/ee/api/members.html#valid-access-levels
ACCESS_MAINTAINER = 40
ACCESS_OWNER = 50


class GitLabClient:
    def __init__(
        self, base_url: str, token: str, *, timeout: float = 30.0
    ) -> None:
        self._http = HTTPClient(
            f"{base_url.rstrip('/')}/api/v4",
            headers={
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
                "User-Agent": f"git-sync/{VERSION}",
            },
            timeout=timeout,
        )

    def me(self) -> GitLabUser:
        data, _ = self._http.get("/user")
        return GitLabUser(id=int(data["id"]), username=str(data["username"]))

    def list_projects(
        self,
        *,
        min_access_level: int = ACCESS_MAINTAINER,
        include_statistics: bool = True,
    ) -> Iterator[GitLabProject]:
        params: dict[str, Any] = {
            "membership": "true",
            "min_access_level": min_access_level,
            "statistics": "true" if include_statistics else "false",
            "order_by": "id",
            "sort": "asc",
        }
        for item in self._http.paginate("/projects", params=params):
            yield _project_from_dict(item)

    def get_languages(self, project_id: int) -> dict[str, float]:
        data, _ = self._http.get(f"/projects/{project_id}/languages")
        if not data:
            return {}
        return {str(k): float(v) for k, v in data.items()}

    def get_project(self, path_or_id: str | int) -> GitLabProject:
        encoded = urllib.parse.quote(str(path_or_id), safe="")
        data, _ = self._http.get(f"/projects/{encoded}")
        return _project_from_dict(data)

    def create_project(
        self,
        *,
        name: str,
        visibility: str = "public",
        default_branch: str = "main",
    ) -> GitLabProject:
        body: dict[str, Any] = {
            "name": name,
            "path": name,
            "visibility": visibility,
            "default_branch": default_branch,
            "initialize_with_readme": False,
        }
        data, _ = self._http.post("/projects", json_body=body)
        return _project_from_dict(data)

    def get_file(
        self, project_id: int, file_path: str, *, ref: str,
    ) -> dict[str, str] | None:
        encoded = urllib.parse.quote(file_path, safe="")
        try:
            data, _ = self._http.get(
                f"/projects/{project_id}/repository/files/{encoded}",
                params={"ref": ref},
            )
        except HTTPError as e:
            if e.status == 404:
                return None
            raise
        return {
            "content": str(data.get("content") or ""),
            "blob_id": str(data.get("blob_id") or ""),
            "last_commit_id": str(data.get("last_commit_id") or ""),
        }

    def put_file(
        self,
        project_id: int,
        file_path: str,
        content: str,
        *,
        branch: str,
        commit_message: str,
        author_name: str,
        author_email: str,
        last_commit_id: str | None = None,
    ) -> None:
        encoded = urllib.parse.quote(file_path, safe="")
        body: dict[str, Any] = {
            "branch": branch,
            "content": content,
            "commit_message": commit_message,
            "author_name": author_name,
            "author_email": author_email,
        }
        if last_commit_id:
            body["last_commit_id"] = last_commit_id
            self._http.put(
                f"/projects/{project_id}/repository/files/{encoded}",
                json_body=body,
            )
        else:
            self._http.post(
                f"/projects/{project_id}/repository/files/{encoded}",
                json_body=body,
            )

    def list_user_events(self, user_id: int, limit: int = 50) -> list[GitLabEvent]:
        events: list[GitLabEvent] = []
        for item in self._http.paginate(
            f"/users/{user_id}/events",
            params={},
            per_page=min(limit, 100),
        ):
            events.append(_event_from_dict(item))
            if len(events) >= limit:
                break
        return events


def _project_from_dict(d: dict[str, Any]) -> GitLabProject:
    stats = d.get("statistics") or {}
    namespace = d.get("namespace") or {}
    return GitLabProject(
        id=int(d["id"]),
        path_with_namespace=str(d["path_with_namespace"]),
        name=str(d["name"]),
        description=str(d.get("description") or ""),
        visibility=str(d["visibility"]),
        default_branch=(
            str(d["default_branch"]) if d.get("default_branch") else None
        ),
        last_activity_at=str(d["last_activity_at"]),
        archived=bool(d.get("archived", False)),
        size_bytes=int(stats.get("repository_size") or 0),
        namespace_kind=str(namespace.get("kind") or "group"),
    )


def _event_from_dict(d: dict[str, Any]) -> GitLabEvent:
    return GitLabEvent(
        id=int(d["id"]),
        action_name=str(d.get("action_name") or ""),
        created_at=str(d["created_at"]),
        target_type=(str(d["target_type"]) if d.get("target_type") else None),
        target_title=(str(d["target_title"]) if d.get("target_title") else None),
        project_id=(int(d["project_id"]) if d.get("project_id") is not None else None),
    )
