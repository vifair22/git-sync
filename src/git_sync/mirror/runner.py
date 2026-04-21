"""Mirror orchestration: enumerate, reconcile, execute per-repo with isolation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .. import log
from ..clients.github import GitHubClient, GitHubRepo
from ..clients.gitlab import GitLabClient
from ..clients.http import HTTPError
from ..state import RepoState, State
from . import reconcile
from .git import GitOps, build_github_push_url, build_gitlab_clone_url
from .reconcile import MirrorAction


def _in_excluded_group(path: str, excluded: tuple[str, ...]) -> bool:
    if not excluded:
        return False
    first_segment = path.split("/", 1)[0]
    return first_segment in excluded

_logger = log.get("git_sync.mirror")


@dataclass
class MirrorResult:
    successes: int = 0
    failures: int = 0
    skipped: int = 0


class MirrorRunner:
    def __init__(
        self,
        *,
        gitlab_client: GitLabClient,
        gitlab_url: str,
        gitlab_token: str,
        github_client: GitHubClient,
        github_owner: str,
        github_token: str,
        git_ops: GitOps,
        state: State,
        exclude_groups: tuple[str, ...] = (),
        profile_gitlab_path: str | None = None,
        mirror_private: bool = False,
        only_group_owned: bool = False,
    ) -> None:
        self.gitlab = gitlab_client
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token
        self.github = github_client
        self.github_owner = github_owner
        self.github_token = github_token
        self.git_ops = git_ops
        self.state = state
        self.exclude_groups = exclude_groups
        self.profile_gitlab_path = profile_gitlab_path
        self.mirror_private = mirror_private
        self.only_group_owned = only_group_owned

    def run(self, *, dry_run: bool = False) -> MirrorResult:
        _logger.info("enumerating gitlab projects")
        projects = list(self.gitlab.list_projects())
        total = len(projects)
        projects = [
            p for p in projects
            if p.path_with_namespace != self.profile_gitlab_path
            and not _in_excluded_group(p.path_with_namespace, self.exclude_groups)
            and not (self.only_group_owned and p.namespace_kind == "user")
        ]
        if len(projects) != total:
            _logger.info(
                "gitlab: %d projects (%d after excluding profile repo + groups)",
                total, len(projects),
            )
        else:
            _logger.info("gitlab: %d projects", total)

        _logger.info("enumerating github repos")
        github_repos = {r.name: r for r in self.github.list_repos()}
        _logger.info("github: %d repos", len(github_repos))

        scoped_state = self._scope_state_for_reconcile()
        p = reconcile.plan(
            projects, github_repos, scoped_state,
            mirror_private=self.mirror_private,
        )

        for name, paths in p.collisions.items():
            _logger.warning(
                "github name collision on %r across gitlab paths: %s",
                name, ", ".join(paths),
            )
        for msg in p.skipped:
            _logger.debug("skipped: %s", msg)

        result = MirrorResult(skipped=len(p.skipped))
        for action in p.actions:
            if dry_run:
                _logger.info(
                    "DRY-RUN %s -> %s (private=%s, mirror=%s)",
                    action.github_name, action.reason,
                    action.desired_private, action.mirror_data,
                )
                continue
            try:
                self._execute(action, github_repos)
                result.successes += 1
            except Exception as e:  # noqa: BLE001 - per-action isolation
                result.failures += 1
                _logger.error(
                    "action for %s failed: %s", action.github_name, e,
                )
                self._record_error(action, str(e))

        return result

    def _execute(
        self, action: MirrorAction, github_repos: dict[str, GitHubRepo]
    ) -> None:
        gh = github_repos.get(action.github_name)
        p = action.project

        if gh is None:
            if p is None:
                _logger.warning(
                    "cannot hide orphan %s: not present on github",
                    action.github_name,
                )
                return
            gh = self.github.create_repo(
                action.github_name,
                private=action.desired_private,
                description=p.description,
            )
            _logger.info(
                "created %s on github (%s)",
                gh.name, "private" if gh.private else "public",
            )
        elif gh.private != action.desired_private:
            gh = self.github.update_repo(
                self.github_owner,
                action.github_name,
                private=action.desired_private,
            )
            _logger.info(
                "flipped %s visibility to %s",
                gh.name, "private" if gh.private else "public",
            )

        source_digest: str | None = None
        if action.mirror_data and p is not None:
            source = build_gitlab_clone_url(
                self.gitlab_url, self.gitlab_token, p.path_with_namespace,
            )
            source_digest = self.git_ops.fetch(p.id, source)
            existing_state = self.state.repos.get(str(p.id))
            last_digest = (
                existing_state.last_sync_source_digest if existing_state else None
            )

            both_archived = p.archived and gh.archived
            source_unchanged = (
                last_digest is not None and last_digest == source_digest
            )
            skip_push = both_archived or source_unchanged

            if skip_push:
                _logger.info(
                    "%s: %s; skipping push",
                    action.github_name,
                    "both sides archived" if both_archived
                    else "source refs unchanged since last sync",
                )
            else:
                if gh.archived:
                    gh = self.github.update_repo(
                        self.github_owner, action.github_name, archived=False,
                    )
                    _logger.info(
                        "unarchived %s so push can proceed",
                        action.github_name,
                    )

                dest = build_github_push_url(
                    self.github_token, self.github_owner, action.github_name,
                )
                self.git_ops.push(p.id, dest)
                _logger.info(
                    "mirrored %s -> %s",
                    p.path_with_namespace, action.github_name,
                )

                if p.default_branch and gh.default_branch != p.default_branch:
                    try:
                        gh = self.github.update_repo(
                            self.github_owner,
                            action.github_name,
                            default_branch=p.default_branch,
                        )
                        _logger.info(
                            "set default_branch of %s to %s",
                            action.github_name, p.default_branch,
                        )
                    except HTTPError as e:
                        if e.status == 422:
                            _logger.warning(
                                "could not set default_branch of %s "
                                "(probably an empty repo); continuing",
                                action.github_name,
                            )
                        else:
                            raise

            if gh.archived != p.archived:
                gh = self.github.update_repo(
                    self.github_owner, action.github_name, archived=p.archived,
                )
                _logger.info(
                    "%s github repo %s to match gitlab",
                    "archived" if p.archived else "unarchived",
                    action.github_name,
                )

        if p is not None:
            prior = self.state.repos.get(str(p.id))
            self.state.repos[str(p.id)] = RepoState(
                gitlab_id=p.id,
                gitlab_path=p.path_with_namespace,
                github_name=action.github_name,
                last_known_visibility=(
                    "private" if action.desired_private else "public"
                ),
                last_sync_utc=datetime.now(timezone.utc).isoformat(),
                last_error=None,
                last_sync_source_digest=(
                    source_digest
                    if source_digest is not None
                    else (prior.last_sync_source_digest if prior else None)
                ),
            )

    def _scope_state_for_reconcile(self) -> State:
        """Return a State view with excluded-group repos hidden from reconcile.

        Hiding them means reconcile never emits an orphan "hide on github" for a
        repo the user explicitly told us to leave alone.
        """
        if not self.exclude_groups:
            return self.state
        scoped = State()
        scoped.profile = self.state.profile
        scoped.repos = {
            k: v for k, v in self.state.repos.items()
            if not _in_excluded_group(v.gitlab_path, self.exclude_groups)
        }
        return scoped

    def _record_error(self, action: MirrorAction, msg: str) -> None:
        if action.project is None:
            return
        key = str(action.project.id)
        existing = self.state.repos.get(key)
        if existing is not None:
            self.state.repos[key] = RepoState(
                gitlab_id=existing.gitlab_id,
                gitlab_path=existing.gitlab_path,
                github_name=existing.github_name,
                last_known_visibility=existing.last_known_visibility,
                last_sync_utc=existing.last_sync_utc,
                last_error=msg,
            )
