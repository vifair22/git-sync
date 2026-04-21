"""Tests for git subprocess operations.

Uses real local bare repos over file:// URLs; no network required.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from git_sync.mirror.git import (
    GitError,
    GitOps,
    build_github_push_url,
    build_gitlab_clone_url,
    scrub_url,
)


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    # Git commands run inside these tests commit via the `work` checkout below;
    # we set identity via env so the host's config isn't required.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e")


def _run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _make_bare(path: Path) -> None:
    _run(["git", "init", "--bare", "--initial-branch=main", str(path)])


def _seed_source(source: Path, work: Path, *, filename="README.md", content="hi") -> str:
    """Initialise a bare `source` with one commit pushed via a working clone."""
    _make_bare(source)
    _run(["git", "clone", str(source), str(work)])
    (work / filename).write_text(content)
    _run(["git", "-C", str(work), "add", filename])
    _run(["git", "-C", str(work), "commit", "-m", f"add {filename}"])
    _run(["git", "-C", str(work), "push", "origin", "main"])
    out = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "main"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_scrub_url_strips_credentials():
    assert scrub_url("https://oauth2:TOKEN@host/repo.git") == "https://host/repo.git"
    assert scrub_url("http://user:pw@h:8080/r.git") == "http://h:8080/r.git"
    assert scrub_url("https://host/repo.git") == "https://host/repo.git"


def test_build_gitlab_clone_url_embeds_token():
    url = build_gitlab_clone_url(
        "https://git.example.com", "mytoken", "alice/foo",
    )
    assert url == "https://oauth2:mytoken@git.example.com/alice/foo.git"


def test_build_github_push_url_embeds_token():
    assert (
        build_github_push_url("tkn", "alice", "foo")
        == "https://oauth2:tkn@github.com/alice/foo.git"
    )


def test_build_gitlab_clone_url_quotes_special_chars():
    url = build_gitlab_clone_url(
        "https://git.example.com", "tok/with+chars", "alice/foo",
    )
    assert "tok%2Fwith%2Bchars" in url


def test_mirror_copies_refs(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    dest = tmp_path / "dest.git"
    cache = tmp_path / "cache"
    sha = _seed_source(source, work)
    _make_bare(dest)

    GitOps(cache).mirror(42, f"file://{source}", f"file://{dest}")

    out = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == sha
    assert (cache / "42.git").is_dir()


def test_mirror_is_idempotent(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    dest = tmp_path / "dest.git"
    cache = tmp_path / "cache"
    _seed_source(source, work)
    _make_bare(dest)

    ops = GitOps(cache)
    ops.mirror(42, f"file://{source}", f"file://{dest}")
    ops.mirror(42, f"file://{source}", f"file://{dest}")


def test_mirror_picks_up_new_commits(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    dest = tmp_path / "dest.git"
    cache = tmp_path / "cache"
    _seed_source(source, work)
    _make_bare(dest)

    ops = GitOps(cache)
    ops.mirror(42, f"file://{source}", f"file://{dest}")

    (work / "another.md").write_text("more")
    _run(["git", "-C", str(work), "add", "another.md"])
    _run(["git", "-C", str(work), "commit", "-m", "more"])
    _run(["git", "-C", str(work), "push", "origin", "main"])

    ops.mirror(42, f"file://{source}", f"file://{dest}")

    ls = subprocess.run(
        ["git", "-C", str(dest), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert len(ls.stdout.strip().splitlines()) == 2


def test_mirror_raises_git_error_on_bad_source(tmp_path):
    dest = tmp_path / "dest.git"
    _make_bare(dest)
    ops = GitOps(tmp_path / "cache")
    with pytest.raises(GitError):
        ops.mirror(1, f"file://{tmp_path}/does-not-exist", f"file://{dest}")


def test_git_error_message_scrubs_tokens(tmp_path):
    ops = GitOps(tmp_path / "cache")
    with pytest.raises(GitError) as exc:
        ops.mirror(
            1,
            "https://oauth2:SECRET@127.0.0.1:1/nope.git",
            "https://oauth2:SECRET@127.0.0.1:1/nope.git",
        )
    assert "SECRET" not in str(exc.value)


def test_mirror_handles_pruned_branch(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    dest = tmp_path / "dest.git"
    cache = tmp_path / "cache"
    _seed_source(source, work)
    _make_bare(dest)
    _run(["git", "-C", str(work), "checkout", "-b", "feature"])
    (work / "f.md").write_text("f")
    _run(["git", "-C", str(work), "add", "f.md"])
    _run(["git", "-C", str(work), "commit", "-m", "feature"])
    _run(["git", "-C", str(work), "push", "origin", "feature"])

    ops = GitOps(cache)
    ops.mirror(1, f"file://{source}", f"file://{dest}")

    # Delete the feature branch upstream; next mirror run should prune from dest.
    _run(["git", "-C", str(work), "push", "origin", "--delete", "feature"])
    ops.mirror(1, f"file://{source}", f"file://{dest}")

    branches = subprocess.run(
        ["git", "-C", str(dest), "branch"],
        capture_output=True, text=True, check=True,
    )
    assert "feature" not in branches.stdout


def test_has_blob_over_returns_false_for_small_repo(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    _seed_source(source, work)

    assert GitOps(tmp_path / "cache")._has_blob_over(source, 1) is False


def test_has_blob_over_detects_large_blob(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    _seed_source(source, work)
    # Append a 2 MB file and push it.
    (work / "big.dat").write_bytes(b"X" * (2 * 1024 * 1024))
    _run(["git", "-C", str(work), "add", "big.dat"])
    _run(["git", "-C", str(work), "commit", "-m", "big"])
    _run(["git", "-C", str(work), "push", "origin", "main"])

    ops = GitOps(tmp_path / "cache")
    assert ops._has_blob_over(source, 1) is True
    assert ops._has_blob_over(source, 10) is False


def test_mirror_with_threshold_but_no_large_blobs_skips_filter(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    dest = tmp_path / "dest.git"
    cache = tmp_path / "cache"
    _seed_source(source, work)
    _make_bare(dest)

    ops = GitOps(cache, strip_blobs_larger_than_mb=10)
    ops.mirror(42, f"file://{source}", f"file://{dest}")

    assert (cache / "42.git").is_dir()
    assert not (cache / "42.filtered.git").exists()


def _filter_repo_available():
    venv_bin = Path(sys.executable).parent
    return (
        shutil.which("git-filter-repo") is not None
        or (venv_bin / "git-filter-repo").is_file()
    )


@pytest.mark.skipif(not _filter_repo_available(), reason="git-filter-repo not installed")
def test_mirror_strips_large_blob_and_pushes_filtered(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    dest = tmp_path / "dest.git"
    cache = tmp_path / "cache"
    _seed_source(source, work)
    (work / "big.dat").write_bytes(b"Y" * (2 * 1024 * 1024))
    _run(["git", "-C", str(work), "add", "big.dat"])
    _run(["git", "-C", str(work), "commit", "-m", "add big"])
    _run(["git", "-C", str(work), "push", "origin", "main"])
    _make_bare(dest)

    ops = GitOps(cache, strip_blobs_larger_than_mb=1)
    ops.mirror(42, f"file://{source}", f"file://{dest}")

    assert (cache / "42.filtered.git").is_dir()
    log_files = subprocess.run(
        ["git", "-C", str(dest), "log", "--all", "--name-only", "--pretty="],
        capture_output=True, text=True, check=True,
    )
    assert "big.dat" not in log_files.stdout
    assert "README.md" in log_files.stdout


@pytest.mark.skipif(not _filter_repo_available(), reason="git-filter-repo not installed")
def test_mirror_filter_is_deterministic_across_runs(tmp_path):
    source = tmp_path / "source.git"
    work = tmp_path / "work"
    dest = tmp_path / "dest.git"
    cache = tmp_path / "cache"
    _seed_source(source, work)
    (work / "big.dat").write_bytes(b"Z" * (2 * 1024 * 1024))
    _run(["git", "-C", str(work), "add", "big.dat"])
    _run(["git", "-C", str(work), "commit", "-m", "big"])
    _run(["git", "-C", str(work), "push", "origin", "main"])
    _make_bare(dest)

    ops = GitOps(cache, strip_blobs_larger_than_mb=1)
    ops.mirror(42, f"file://{source}", f"file://{dest}")
    sha_after_first = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    ops.mirror(42, f"file://{source}", f"file://{dest}")
    sha_after_second = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert sha_after_first == sha_after_second


def _has_git():
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not installed")
