"""Command-line entry point for git-sync."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import VERSION, config, log, state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-sync",
        description="Mirror GitLab repos to GitHub and generate profile READMEs.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (default: $GIT_SYNC_CONFIG or /etc/git-sync/config.toml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without executing them.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mirror", help="Run one mirror pass and exit.")
    profile_parser = sub.add_parser(
        "profile", help="Run one profile generation pass and exit.",
    )
    profile_parser.add_argument(
        "--refresh-languages",
        action="store_true",
        help=(
            "Clear the cached language LOC counts before running so cloc "
            "recomputes from scratch."
        ),
    )
    sub.add_parser("run", help="Long-running daemon: mirror and profile on schedule.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    log.configure(cfg.logging.level)
    logger = log.get("git_sync.cli")
    logger.debug("loaded config (version=%s)", VERSION)

    dispatch = {
        "mirror": _cmd_mirror,
        "profile": _cmd_profile,
        "run": _cmd_run,
    }
    extra: dict = {}
    if args.command == "profile":
        extra["refresh_languages"] = args.refresh_languages
    return dispatch[args.command](cfg, dry_run=args.dry_run, **extra)


def _cmd_mirror(cfg: config.Config, *, dry_run: bool) -> int:
    from .clients.github import GitHubClient
    from .clients.gitlab import GitLabClient
    from .mirror.git import GitOps
    from .mirror.runner import MirrorRunner

    logger = log.get("git_sync.mirror")
    logger.info("mirror pass starting%s", " (dry-run)" if dry_run else "")
    current = state.load(cfg.paths.state)
    runner = MirrorRunner(
        gitlab_client=GitLabClient(cfg.gitlab.url, cfg.gitlab.token),
        gitlab_url=cfg.gitlab.url,
        gitlab_token=cfg.gitlab.token,
        github_client=GitHubClient(cfg.github.token),
        github_owner=cfg.github.owner,
        github_token=cfg.github.token,
        git_ops=GitOps(
            cfg.paths.cache,
            strip_blobs_larger_than_mb=cfg.mirror.strip_blobs_larger_than_mb,
        ),
        state=current,
        exclude_groups=cfg.mirror.exclude_groups,
        profile_gitlab_path=cfg.profile.gitlab_path,
        mirror_private=cfg.mirror.mirror_private_repos,
        only_group_owned=cfg.mirror.only_group_owned,
    )
    result = runner.run(dry_run=dry_run)
    if not dry_run:
        state.save(cfg.paths.state, current)
    logger.info(
        "mirror done: successes=%d failures=%d skipped=%d",
        result.successes, result.failures, result.skipped,
    )
    return 1 if result.failures else 0


def _cmd_profile(
    cfg: config.Config, *, dry_run: bool, refresh_languages: bool = False,
) -> int:
    from .clients.github import GitHubClient
    from .clients.gitlab import GitLabClient
    from .profile.runner import ProfileRunner

    logger = log.get("git_sync.profile")
    logger.info("profile pass starting%s", " (dry-run)" if dry_run else "")
    current = state.load(cfg.paths.state)
    if refresh_languages:
        logger.info("clearing cached language stats (--refresh-languages)")
        current.profile.language_cache = {}
        current.profile.language_cache_utc = None
    runner = ProfileRunner(
        gitlab_client=GitLabClient(cfg.gitlab.url, cfg.gitlab.token),
        gitlab_url=cfg.gitlab.url,
        gitlab_profile_path=cfg.profile.gitlab_path,
        github_client=GitHubClient(cfg.github.token),
        github_owner=cfg.github.owner,
        github_profile_repo=cfg.profile.github_repo,
        author_name=cfg.author.name,
        author_email=cfg.author.email,
        about_path=cfg.paths.about,
        github_disclaimer=cfg.profile.github_disclaimer,
        state=current,
        top_n_languages=cfg.profile.top_n_languages,
        recent_activity_count=cfg.profile.recent_activity_count,
        recent_repos_count=cfg.profile.recent_repos_count,
        cache_dir=cfg.paths.cache,
        highlights=cfg.profile.highlights,
    )
    result = runner.run(dry_run=dry_run)
    if not dry_run:
        state.save(cfg.paths.state, current)
    logger.info(
        "profile done: gitlab=%s github=%s",
        "published" if result.published_gitlab
        else "unchanged" if result.skipped_gitlab_unchanged
        else "failed" if result.failed_gitlab
        else "skipped",
        "published" if result.published_github
        else "unchanged" if result.skipped_github_unchanged
        else "failed" if result.failed_github
        else "skipped",
    )
    return 1 if (result.failed_gitlab or result.failed_github) else 0


def _cmd_run(cfg: config.Config, *, dry_run: bool) -> int:
    from datetime import datetime, timedelta, timezone

    from .daemon import SignalStopFlag, Task, run_loop

    logger = log.get("git_sync.run")

    if dry_run:
        logger.info("dry-run daemon: one pass of each task, then exit")
        _cmd_mirror(cfg, dry_run=True)
        _cmd_profile(cfg, dry_run=True)
        return 0

    logger.info(
        "daemon starting: mirror every %dh, profile every %dh",
        cfg.schedule.mirror_interval_hours,
        cfg.schedule.profile_interval_hours,
    )
    start = datetime.now(timezone.utc)
    tasks = [
        Task(
            name="mirror",
            interval=timedelta(hours=cfg.schedule.mirror_interval_hours),
            run=lambda: _cmd_mirror(cfg, dry_run=False),
            next_due=start,
        ),
        Task(
            name="profile",
            interval=timedelta(hours=cfg.schedule.profile_interval_hours),
            run=lambda: _cmd_profile(cfg, dry_run=False),
            next_due=start,
        ),
    ]
    run_loop(tasks, stop_flag=SignalStopFlag())
    logger.info("daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
