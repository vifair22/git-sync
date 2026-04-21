"""Git subprocess wrappers for mirror operations.

All remote URLs passed to git embed credentials (``oauth2:<token>``). Every
command is logged with URLs scrubbed, and any stderr we surface is scrubbed as
well so tokens never reach logs or exceptions.

When ``strip_blobs_larger_than_mb`` is set, repositories containing any blob
over that threshold are rewritten via ``git-filter-repo`` into a sidecar
mirror clone before being pushed to GitHub. This produces a GitHub copy whose
commit SHAs do not match GitLab for affected repos, but avoids GitHub's 100 MB
per-file push ceiling.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

from .. import log

_logger = log.get("git_sync.git")

_CREDS_RE = re.compile(r"(https?://)[^@/\s]+@")


class GitError(Exception):
    pass


def scrub_url(url: str) -> str:
    """Strip userinfo from a URL so it's safe to log."""
    return _CREDS_RE.sub(r"\1", url)


def build_gitlab_clone_url(base_url: str, token: str, path_with_namespace: str) -> str:
    p = urllib.parse.urlparse(base_url)
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    creds = f"oauth2:{urllib.parse.quote(token, safe='')}"
    return f"{p.scheme}://{creds}@{host}/{path_with_namespace}.git"


def build_github_push_url(token: str, owner: str, repo: str) -> str:
    creds = f"oauth2:{urllib.parse.quote(token, safe='')}"
    return f"https://{creds}@github.com/{owner}/{repo}.git"


class GitOps:
    def __init__(
        self,
        cache_dir: Path,
        *,
        strip_blobs_larger_than_mb: int | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.strip_mb = strip_blobs_larger_than_mb

    def fetch(self, project_id: int, source_url: str) -> str:
        """Update the source cache from GitLab. Returns a digest of all refs."""
        source_dir = self.cache_dir / f"{project_id}.git"
        self._update_source(source_dir, source_url)
        return self._source_digest(source_dir)

    def push(self, project_id: int, dest_url: str) -> None:
        """Push the (possibly filtered) source cache to GitHub."""
        source_dir = self.cache_dir / f"{project_id}.git"
        push_from = source_dir
        if self.strip_mb and self._has_blob_over(source_dir, self.strip_mb):
            filtered_dir = self.cache_dir / f"{project_id}.filtered.git"
            _logger.info(
                "project %d has blobs > %d MB; rewriting history for github push",
                project_id, self.strip_mb,
            )
            self._rebuild_filtered(source_dir, filtered_dir, self.strip_mb)
            push_from = filtered_dir
        self._run(
            ["git", "-C", str(push_from), "push", "--mirror", dest_url],
        )

    def mirror(self, project_id: int, source_url: str, dest_url: str) -> None:
        """Convenience: fetch + push. Used where digest-based skipping isn't wired."""
        self.fetch(project_id, source_url)
        self.push(project_id, dest_url)

    def _source_digest(self, repo_dir: Path) -> str:
        proc = subprocess.run(
            [
                "git", "-C", str(repo_dir), "for-each-ref",
                "--format=%(objectname) %(refname)",
            ],
            capture_output=True, text=True, check=False, env=_subprocess_env(),
        )
        if proc.returncode != 0:
            raise GitError(
                f"git for-each-ref failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()}",
            )
        # Sort so the digest is stable regardless of iteration order.
        lines = sorted(proc.stdout.splitlines())
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def _update_source(self, repo_dir: Path, source_url: str) -> None:
        if repo_dir.exists():
            self._run(
                ["git", "-C", str(repo_dir), "remote", "set-url", "origin", source_url],
            )
            self._run(
                ["git", "-C", str(repo_dir), "remote", "update", "--prune"],
            )
        else:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._run(["git", "clone", "--mirror", source_url, str(repo_dir)])

    def _has_blob_over(self, repo_dir: Path, mb_threshold: int) -> bool:
        threshold_bytes = mb_threshold * 1024 * 1024
        proc = subprocess.run(
            [
                "git", "-C", str(repo_dir), "cat-file",
                "--batch-check=%(objecttype) %(objectsize)",
                "--batch-all-objects", "--unordered",
            ],
            capture_output=True, text=True, check=False, env=_subprocess_env(),
        )
        if proc.returncode != 0:
            raise GitError(
                f"git cat-file failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()}",
            )
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] != "blob":
                continue
            try:
                if int(parts[1]) > threshold_bytes:
                    return True
            except ValueError:
                continue
        return False

    def _rebuild_filtered(
        self, source_dir: Path, filtered_dir: Path, mb_threshold: int,
    ) -> None:
        if filtered_dir.exists():
            shutil.rmtree(filtered_dir)
        self._run(
            ["git", "clone", "--mirror", str(source_dir), str(filtered_dir)],
        )
        self._run(
            [
                "git", "-C", str(filtered_dir),
                "filter-repo",
                "--strip-blobs-bigger-than", f"{mb_threshold}M",
                "--force",
            ],
        )

    def _run(self, cmd: list[str]) -> None:
        safe_cmd = [scrub_url(a) for a in cmd]
        _logger.debug("git: %s", shlex.join(safe_cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            env=_subprocess_env(),
        )
        if proc.returncode != 0:
            stderr = scrub_url(proc.stderr).strip()
            raise GitError(
                f"{shlex.join(safe_cmd)} failed (exit {proc.returncode}): {stderr}",
            )


def _subprocess_env() -> dict[str, str]:
    """Return a copy of the environment with the current venv's bin on PATH.

    ``git-filter-repo`` may be installed in the venv (pip-installed) rather
    than system-wide; `git` locates subcommands via PATH, so we prepend the
    venv's bin to ensure invocations like ``git filter-repo`` resolve.
    """
    env = os.environ.copy()
    venv_bin = os.path.dirname(sys.executable)
    path_parts = env.get("PATH", "").split(os.pathsep)
    if venv_bin and venv_bin not in path_parts:
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    return env
