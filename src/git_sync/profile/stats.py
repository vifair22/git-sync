"""Profile data aggregation.

Given a GitLab client and the persisted state, produce a ``ProfileData`` object
suitable for rendering a profile README.

Language totals are computed from **lines of code** via ``cloc`` over each
project's local bare clone in ``cache_dir``. This is accurate and immune to
vendored/data-file bloat that distorted the older GitLab byte-percentage
approach. Totals are cached in ``state.profile.language_cache`` for
``LANGUAGE_CACHE_TTL`` to avoid rerunning cloc on every pass.

If ``cache_dir`` is not provided (e.g. in tests that don't set up real
clones) the code falls back to GitLab's percentage-weighted byte estimate.

Recent-repo and recent-activity sections are *public-only* — private project
names would leak via event titles and repo lists otherwise. Language totals
include private repos (that data is aggregated and does not reveal names).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import log
from ..clients.gitlab import GitLabEvent, GitLabProject
from ..config import HighlightEntry
from ..state import ProfileState, State

_logger = log.get("git_sync.profile.stats")

LANGUAGE_CACHE_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class LanguageStat:
    name: str
    bytes: int
    pct: float


@dataclass(frozen=True)
class RecentRepo:
    path_with_namespace: str
    name: str
    description: str
    last_activity_at: str


@dataclass(frozen=True)
class ProfileData:
    generated_at_utc: str
    total_public_repos: int
    total_all_repos: int
    top_languages: list[LanguageStat]
    recent_activity: list[GitLabEvent]
    recent_repos: list[RecentRepo]
    highlights: tuple[HighlightEntry, ...] = ()


def aggregate(
    gitlab,
    state: State,
    *,
    top_n_languages: int = 8,
    recent_activity_count: int = 20,
    recent_repos_count: int = 5,
    cache_dir: Path | None = None,
    highlights: tuple[HighlightEntry, ...] = (),
    now: datetime | None = None,
) -> ProfileData:
    now = now or datetime.now(timezone.utc)
    projects = list(gitlab.list_projects())
    public = [p for p in projects if p.visibility == "public"]
    public_ids = {p.id for p in public}

    language_bytes = _compute_languages(
        gitlab, projects, state.profile, now, cache_dir=cache_dir,
    )

    user = gitlab.me()
    raw_events = gitlab.list_user_events(
        user.id, limit=max(recent_activity_count * 3, recent_activity_count),
    )
    filtered_events = [
        e for e in raw_events
        if e.project_id is None or e.project_id in public_ids
    ][:recent_activity_count]

    recent_sorted = sorted(
        public, key=lambda p: p.last_activity_at, reverse=True,
    )
    limit = len(recent_sorted) if recent_repos_count <= 0 else recent_repos_count
    recent_repos = [
        RecentRepo(
            path_with_namespace=p.path_with_namespace,
            name=p.name,
            description=p.description,
            last_activity_at=p.last_activity_at,
        )
        for p in recent_sorted[:limit]
    ]

    return ProfileData(
        generated_at_utc=now.isoformat(),
        total_public_repos=len(public),
        total_all_repos=len(projects),
        top_languages=_top_languages(language_bytes, top_n_languages),
        recent_activity=filtered_events,
        recent_repos=recent_repos,
        highlights=highlights,
    )


def _compute_languages(
    gitlab,
    projects: list[GitLabProject],
    profile_state: ProfileState,
    now: datetime,
    *,
    cache_dir: Path | None,
) -> dict[str, int]:
    cached_at = _parse_utc(profile_state.language_cache_utc)
    if (
        cached_at is not None
        and now - cached_at < LANGUAGE_CACHE_TTL
        and profile_state.language_cache
    ):
        _logger.info(
            "language cache hit (%d languages, age=%s)",
            len(profile_state.language_cache),
            now - cached_at,
        )
        return dict(profile_state.language_cache)

    if cache_dir is not None and cache_dir.exists():
        totals = _compute_loc_via_cloc(projects, cache_dir)
    else:
        _logger.info(
            "cache_dir not available; falling back to GitLab byte estimate",
        )
        totals = _compute_bytes_via_gitlab(gitlab, projects)

    profile_state.language_cache = dict(totals)
    profile_state.language_cache_utc = now.isoformat()
    return dict(totals)


def _compute_loc_via_cloc(
    projects: list[GitLabProject], cache_dir: Path,
) -> Counter[str]:
    _logger.info(
        "refreshing language LOC via cloc across %d projects", len(projects),
    )
    totals: Counter[str] = Counter()
    for p in projects:
        if p.size_bytes > _SKIP_REPO_SIZE_BYTES:
            _logger.info(
                "skipping %s from LOC stats: repo is %d MB (exceeds %d MB cap)",
                p.path_with_namespace,
                p.size_bytes // (1024 * 1024),
                _SKIP_REPO_SIZE_BYTES // (1024 * 1024),
            )
            continue
        bare = cache_dir / f"{p.id}.git"
        if not bare.is_dir():
            _logger.debug(
                "skipping %s: cache dir %s missing", p.path_with_namespace, bare,
            )
            continue
        try:
            per_lang = _cloc_project(bare)
        except FileNotFoundError as e:
            raise RuntimeError(
                "cloc binary not found; install cloc to use LOC-based stats",
            ) from e
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloc for %s failed: %s", p.path_with_namespace, e)
            continue
        for lang, loc in per_lang.items():
            totals[lang] += loc
    return totals


_SKIP_REPO_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Languages cloc reports that we treat as content/data, not programming work.
_NON_CODE_LANGUAGES: frozenset[str] = frozenset({
    "Text", "Markdown", "reStructuredText", "AsciiDoc", "Org",
    "JSON", "JSON5", "YAML", "TOML", "INI", "CSV", "TSV",
    "XML", "SVG", "DTD",
    "Rich Text Format",
    "Jupyter Notebook",
    "diff", "Properties File",
})


def _cloc_project(bare_dir: Path) -> dict[str, int]:
    # Extract under the cache's parent dir (disk-backed) instead of /tmp
    # (often tmpfs) so big repos don't blow up RAM.
    tmp_parent = bare_dir.parent
    with tempfile.TemporaryDirectory(
        prefix="git-sync-loc-", dir=str(tmp_parent),
    ) as tmp:
        archive = subprocess.Popen(
            ["git", "-C", str(bare_dir), "archive", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        tar = subprocess.Popen(
            ["tar", "-x", "-C", tmp],
            stdin=archive.stdout,
            stderr=subprocess.DEVNULL,
        )
        if archive.stdout is not None:
            archive.stdout.close()
        tar_rc = tar.wait()
        archive_rc = archive.wait()
        if archive_rc != 0 or tar_rc != 0:
            # Most commonly an empty repo (no HEAD) or a race; skip it.
            return {}

        proc = subprocess.run(
            ["cloc", "--json", "--quiet", tmp],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}

    result: dict[str, int] = {}
    for lang, stats in data.items():
        if lang in ("header", "SUM") or not isinstance(stats, dict):
            continue
        if lang in _NON_CODE_LANGUAGES:
            continue
        code = stats.get("code")
        if isinstance(code, int) and code > 0:
            result[lang] = code
    return result


def _compute_bytes_via_gitlab(
    gitlab, projects: list[GitLabProject],
) -> Counter[str]:
    _logger.info(
        "refreshing language byte estimate across %d projects", len(projects),
    )
    totals: Counter[str] = Counter()
    for p in projects:
        if p.size_bytes <= 0:
            continue
        try:
            pct_by_lang = gitlab.get_languages(p.id)
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "languages for %s failed: %s", p.path_with_namespace, e,
            )
            continue
        for lang, pct in pct_by_lang.items():
            totals[lang] += int(p.size_bytes * pct / 100.0)
    return totals


def _top_languages(totals: dict[str, int], n: int) -> list[LanguageStat]:
    total = sum(totals.values())
    if total <= 0 or n <= 0:
        return []
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [
        LanguageStat(name=name, bytes=b, pct=100.0 * b / total)
        for name, b in ranked
    ]


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
