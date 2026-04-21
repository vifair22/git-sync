"""Reconciliation planner.

Pure function: given the enumerated GitLab projects, existing GitHub repos, and
our persisted state, produce a list of ``MirrorAction`` items that, when
executed, bring GitHub into the desired state.

Policy (MVP):

* GitHub repo public iff the corresponding GitLab project exists AND is public.
* Private GitLab projects are *not* created on GitHub.
* If a previously-mirrored GitLab project disappears (or flips private), we
  only hide the GitHub mirror (flip to private). We never delete.
* Mapping: GitHub repo name = last path segment of the GitLab project
  (``vifair22/group/foo`` -> ``foo``). Collisions are detected and skipped.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..clients.github import GitHubRepo
from ..clients.gitlab import GitLabProject
from ..state import State


@dataclass(frozen=True)
class MirrorAction:
    github_name: str
    desired_private: bool
    mirror_data: bool
    project: GitLabProject | None = None  # None = orphan (hide-only)
    reason: str = ""


@dataclass
class Plan:
    actions: list[MirrorAction] = field(default_factory=list)
    collisions: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def derive_github_name(project: GitLabProject) -> str:
    """Derive the GitHub repo name from a GitLab project path.

    Uses the final path segment only. ``vifair22/foo`` -> ``foo``;
    ``vifair22/group/bar`` -> ``bar``.
    """
    return project.path_with_namespace.rsplit("/", 1)[-1]


def plan(
    projects: list[GitLabProject],
    github_repos: dict[str, GitHubRepo],
    state: State,
    *,
    mirror_private: bool = False,
) -> Plan:
    result = Plan()

    by_name: dict[str, list[GitLabProject]] = defaultdict(list)
    for p in projects:
        by_name[derive_github_name(p)].append(p)
    result.collisions = {
        name: [p.path_with_namespace for p in ps]
        for name, ps in by_name.items()
        if len(ps) > 1
    }

    seen_gl_ids: set[int] = set()

    for p in projects:
        github_name = derive_github_name(p)
        if github_name in result.collisions:
            result.skipped.append(
                f"{p.path_with_namespace}: collision on github name {github_name!r}"
            )
            continue

        seen_gl_ids.add(p.id)
        gh = github_repos.get(github_name)
        is_public = p.visibility == "public"

        if is_public:
            if gh is None:
                reason = "create public + mirror"
            elif gh.private:
                reason = "flip to public + mirror"
            else:
                reason = "mirror"
            result.actions.append(
                MirrorAction(
                    github_name=github_name,
                    desired_private=False,
                    mirror_data=True,
                    project=p,
                    reason=reason,
                ),
            )
        elif mirror_private:
            if gh is None:
                reason = "create private + mirror"
            elif not gh.private:
                reason = "flip to private + mirror"
            else:
                reason = "mirror (private)"
            result.actions.append(
                MirrorAction(
                    github_name=github_name,
                    desired_private=True,
                    mirror_data=True,
                    project=p,
                    reason=reason,
                ),
            )
        else:
            if gh is not None and not gh.private:
                result.actions.append(
                    MirrorAction(
                        github_name=github_name,
                        desired_private=True,
                        mirror_data=False,
                        project=p,
                        reason="gitlab not public: hide github",
                    ),
                )
            else:
                result.skipped.append(
                    f"{p.path_with_namespace}: private on gitlab; nothing to do"
                )

    for gl_id_str, repo_state in state.repos.items():
        try:
            gl_id = int(gl_id_str)
        except ValueError:
            continue
        if gl_id in seen_gl_ids:
            continue
        gh = github_repos.get(repo_state.github_name)
        if gh is None:
            result.skipped.append(
                f"orphan {repo_state.github_name}: already gone from github"
            )
            continue
        if gh.private:
            result.skipped.append(
                f"orphan {repo_state.github_name}: already hidden"
            )
            continue
        result.actions.append(
            MirrorAction(
                github_name=repo_state.github_name,
                desired_private=True,
                mirror_data=False,
                project=None,
                reason=f"gitlab source gone (id={gl_id}): hide github",
            ),
        )

    return result
