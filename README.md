# git-sync

One-way mirror of GitLab repositories to GitHub, plus a generated profile
README that gets published to both platforms on a schedule. Runs as a
long-lived daemon in a container, or one-shot from the CLI.

Source of truth is GitLab. GitHub is a read-only mirror and fully disposable —
the tool will re-create repos, flip visibility, and push-mirror history as
needed to make GitHub reflect the current state of GitLab.

## What it does

- **Mirror pass**: enumerate the authenticated user's GitLab projects (member
  with Maintainer+ access). For each public project, ensure a public GitHub
  repo exists and push its full history (`git push --mirror`). For each
  project that is private on GitLab (or has disappeared), ensure its GitHub
  counterpart, if any, is *private*. Never deletes.
- **Profile pass**: aggregate size-weighted language stats across all your
  repos, grab your recent GitLab activity and recently-updated public repos,
  render a Markdown README, and publish it to `<user>/<user>` on both
  platforms.
- **Daemon**: runs both passes on boot, then on their configured intervals
  (defaults to once per day each). SIGTERM/SIGINT exits cleanly between
  tasks.

## Requirements

- Python 3.11+ (dev); the container ships 3.14.
- `git` available in `$PATH`.
- GitLab personal access token and GitHub personal access token (scopes
  listed in `credentials.txt`).
- A GitLab project *and* a GitHub repo for your profile README — both named
  `<user>/<user>` by convention. git-sync does **not** create these for you.

## Quick start (Docker)

1. Clone and `cp config.toml.example deploy/config/config.toml`, edit it.
2. Write your bio snippet to `deploy/config/about.md` (markdown, included
   verbatim above the stats tables).
3. Create `.env` at the repo root:
   ```
   GITLAB_TOKEN=glpat-…
   GITHUB_TOKEN=ghp_…
   ```
4. `docker buildx build -t git-sync:latest .`
5. `docker compose up -d`

`docker compose logs -f git-sync` shows mirror + profile activity.

## Quick start (local)

```
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python scripts/stamp_version.py release

export GITLAB_TOKEN=... GITHUB_TOKEN=...
export GIT_SYNC_CONFIG=/path/to/config.toml
.venv/bin/git-sync --dry-run mirror
.venv/bin/git-sync --dry-run profile
.venv/bin/git-sync run              # daemon
```

## Commands

| Command | Behaviour |
| --- | --- |
| `git-sync mirror`   | One mirror pass and exit. Returns 1 if any repo failed. |
| `git-sync profile`  | One profile-publish pass and exit. |
| `git-sync run`      | Long-running daemon: both tasks on boot, then on interval. |
| `--dry-run` before any subcommand | Show the plan; make no writes. |
| `--version`         | Print the full build-stamped version string. |

## Configuration

Non-secret config lives in TOML; secrets come from environment variables.
See [`config.toml.example`](./config.toml.example) for the full schema with
comments. See [`credentials.txt`](./credentials.txt) for required PAT scopes.

## Operational detail

For architecture, failure modes, state recovery, and security considerations,
see [`Manual.md`](./Manual.md).

## Development

```
.venv/bin/pytest                     # 140 tests
.venv/bin/pytest --cov=git_sync      # ~96 % coverage
```

Zero runtime dependencies — standard library only (`tomllib`, `json`,
`urllib.request`, `subprocess`). Test deps: `pytest`, `pytest-cov`.

## License

GPL-3.0-or-later. See [`LICENSE`](./LICENSE).
