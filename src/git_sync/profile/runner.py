"""Profile orchestration: aggregate, render for each platform, publish with
content-hash gate so unchanged output is a no-op.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .. import log
from ..clients.github import GitHubClient
from ..clients.gitlab import GitLabClient
from ..clients.http import HTTPError
from ..state import State
from . import render, stats

_logger = log.get("git_sync.profile")


@dataclass
class ProfileResult:
    published_gitlab: bool = False
    published_github: bool = False
    skipped_gitlab_unchanged: bool = False
    skipped_github_unchanged: bool = False
    failed_gitlab: bool = False
    failed_github: bool = False


class ProfileRunner:
    def __init__(
        self,
        *,
        gitlab_client: GitLabClient,
        gitlab_url: str,
        gitlab_profile_path: str,
        github_client: GitHubClient,
        github_owner: str,
        github_profile_repo: str,
        author_name: str,
        author_email: str,
        about_path: Path,
        github_disclaimer: str,
        state: State,
        top_n_languages: int,
        recent_activity_count: int,
        recent_repos_count: int,
        cache_dir: Path | None = None,
        highlights: tuple = (),
    ) -> None:
        self.gitlab = gitlab_client
        self.gitlab_url = gitlab_url
        self.gitlab_profile_path = gitlab_profile_path
        self.github = github_client
        self.github_owner = github_owner
        self.github_profile_repo = github_profile_repo
        self.author_name = author_name
        self.author_email = author_email
        self.about_path = about_path
        self.github_disclaimer = github_disclaimer
        self.state = state
        self.top_n_languages = top_n_languages
        self.recent_activity_count = recent_activity_count
        self.recent_repos_count = recent_repos_count
        self.cache_dir = cache_dir
        self.highlights = highlights

    def run(self, *, dry_run: bool = False) -> ProfileResult:
        about_text = self._read_about()
        data = stats.aggregate(
            self.gitlab, self.state,
            top_n_languages=self.top_n_languages,
            recent_activity_count=self.recent_activity_count,
            recent_repos_count=self.recent_repos_count,
            cache_dir=self.cache_dir,
            highlights=self.highlights,
        )

        gitlab_content = render.render(
            data, about_text=about_text,
            gitlab_base_url=self.gitlab_url,
            disclaimer="",
        )
        github_content = render.render(
            data, about_text=about_text,
            gitlab_base_url=self.gitlab_url,
            disclaimer=self.github_disclaimer,
        )

        result = ProfileResult()
        gitlab_hash = _hash(gitlab_content)
        github_hash = _hash(github_content)

        commit_message = f"profile: update {data.generated_at_utc[:10]}"

        self._handle_gitlab(
            gitlab_content, gitlab_hash, commit_message, result, dry_run,
        )
        self._handle_github(
            github_content, github_hash, commit_message, result, dry_run,
        )

        if not dry_run and (result.published_gitlab or result.published_github):
            self.state.profile.last_publish_utc = data.generated_at_utc

        return result

    def _handle_gitlab(
        self, content, content_hash, commit_message, result, dry_run,
    ) -> None:
        if content_hash == self.state.profile.last_gitlab_hash:
            _logger.info("gitlab profile unchanged; skip publish")
            result.skipped_gitlab_unchanged = True
            return
        if dry_run:
            _logger.info(
                "DRY-RUN gitlab profile would publish (%d bytes, hash=%s)",
                len(content), content_hash[:8],
            )
            return
        try:
            self._publish_gitlab(content, commit_message)
            self.state.profile.last_gitlab_hash = content_hash
            result.published_gitlab = True
            _logger.info("published gitlab profile")
        except Exception as e:  # noqa: BLE001
            result.failed_gitlab = True
            _logger.error("gitlab profile publish failed: %s", e)

    def _handle_github(
        self, content, content_hash, commit_message, result, dry_run,
    ) -> None:
        if content_hash == self.state.profile.last_github_hash:
            _logger.info("github profile unchanged; skip publish")
            result.skipped_github_unchanged = True
            return
        if dry_run:
            _logger.info(
                "DRY-RUN github profile would publish (%d bytes, hash=%s)",
                len(content), content_hash[:8],
            )
            return
        try:
            self._publish_github(content, commit_message)
            self.state.profile.last_github_hash = content_hash
            result.published_github = True
            _logger.info("published github profile")
        except Exception as e:  # noqa: BLE001
            result.failed_github = True
            _logger.error("github profile publish failed: %s", e)

    def _publish_gitlab(self, content: str, commit_message: str) -> None:
        try:
            project = self.gitlab.get_project(self.gitlab_profile_path)
        except HTTPError as e:
            if e.status != 404:
                raise
            name = self.gitlab_profile_path.rsplit("/", 1)[-1]
            _logger.info(
                "creating gitlab profile repo %s", self.gitlab_profile_path,
            )
            project = self.gitlab.create_project(
                name=name, visibility="public", default_branch="main",
            )
            existing = None
        else:
            existing = self.gitlab.get_file(
                project.id, "README.md",
                ref=(project.default_branch or "main"),
            )
        branch = project.default_branch or "main"
        self.gitlab.put_file(
            project.id, "README.md", content,
            branch=branch,
            commit_message=commit_message,
            author_name=self.author_name,
            author_email=self.author_email,
            last_commit_id=(existing["last_commit_id"] if existing else None),
        )

    def _publish_github(self, content: str, commit_message: str) -> None:
        repo = self.github.get_repo(self.github_owner, self.github_profile_repo)
        if repo is None:
            _logger.info(
                "creating github profile repo %s/%s",
                self.github_owner, self.github_profile_repo,
            )
            repo = self.github.create_repo(
                self.github_profile_repo, private=False, description="",
            )
            sha: str | None = None
        else:
            existing = self.github.get_file(
                self.github_owner, self.github_profile_repo, "README.md",
                ref=(repo.default_branch or None),
            )
            sha = existing["sha"] if existing else None
        branch = repo.default_branch or "main"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        self.github.put_file(
            self.github_owner, self.github_profile_repo, "README.md",
            encoded,
            commit_message=commit_message,
            branch=branch,
            author_name=self.author_name,
            author_email=self.author_email,
            sha=sha,
        )

    def _read_about(self) -> str:
        if not self.about_path.exists():
            _logger.warning(
                "about file not found at %s; rendering without bio",
                self.about_path,
            )
            return ""
        return self.about_path.read_text(encoding="utf-8")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
