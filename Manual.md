# git-sync Manual

Operational reference for running git-sync in production. For a getting-started
overview see [`README.md`](./README.md).

---

## Architecture

```
           +-------------------+            +-------------------+
           |   GitLab (source) |            |  GitHub (mirror)  |
           +---------+---------+            +---------+---------+
                     |                                |
              REST + git HTTPS                  REST + git HTTPS
                     |                                |
      +--------------v--------------+ +---------------v--------------+
      |        GitLabClient         | |         GitHubClient         |
      |  list_projects / languages  | |   list_repos / create / patch|
      |  me / events / put_file     | |   get_file / put_file        |
      +--------------+--------------+ +---------------+--------------+
                     \                               /
                      \                             /
                       v                           v
          +--------------------+       +---------------------+
          |  mirror.runner     |       |   profile.runner    |
          |  reconcile -> exec |       |   aggregate -> pub  |
          +---------+----------+       +----------+----------+
                    |                              |
                    +-------------+ +--------------+
                                  v v
                           +---------------+
                           | state (JSON)  |
                           |  + cache/     |
                           +---------------+
```

Two passes share the same HTTP clients and the same state file. Their runners
are independent and can be invoked one-shot from the CLI or scheduled together
by the daemon loop.

## Data flow

### Mirror pass (standard)

1. `GitLabClient.list_projects()` — paginated enumeration of projects the
   authenticated user is a member of at **Maintainer** access level or above
   (`min_access_level=40`). Projects arrive with their statistics block
   populated so repository size is known up front.
2. `GitHubClient.list_repos()` — paginated enumeration of repos owned by the
   authenticated user (follows `Link: rel="next"` headers).
3. `mirror.reconcile.plan(projects, repos, state)` — pure function that
   compares the two sides against the persisted state and emits a list of
   `MirrorAction`s. The decision table:

   | GitLab visibility | GitHub state | Action                       |
   | ----------------- | ------------ | ---------------------------- |
   | public            | absent       | create public + mirror       |
   | public            | public       | mirror                       |
   | public            | private      | flip to public + mirror      |
   | private/internal  | absent       | (skip)                       |
   | private/internal  | public       | flip to private (no mirror)  |
   | private/internal  | private      | (skip)                       |
   | (gone, orphan)    | public       | flip to private              |
   | (gone, orphan)    | private/abs. | (skip)                       |

   A "collision" — two GitLab projects that map to the same GitHub repo name —
   is recorded and both sides are skipped for that name.

4. For each action, `mirror.runner` in order:
   a. Ensures the GitHub repo exists with the desired visibility (creates or
   patches as needed).
   b. If `mirror_data` is true, invokes `mirror.git.GitOps.mirror()`:
      `git clone --mirror` on first run (cached under
      `/var/lib/git-sync/cache/<gitlab_project_id>.git`), or
      `git remote update --prune` followed by `git push --mirror` on
      subsequent runs.
   c. If the GitLab default branch differs from the freshly-pushed GitHub
   default branch, patches it.
   d. Archived state is synced. If both sides are already archived, the push
   is skipped entirely. Otherwise: if the GitHub side is archived it is
   unarchived (so the push can proceed), then the push runs, then the GitHub
   side is re-archived if the GitLab side is archived.

   e. Writes a fresh `RepoState` entry (gitlab_id, gitlab_path, github_name,
   visibility, `last_sync_utc`, `last_error=None`).

   Per-action errors are caught, logged, and recorded on the state entry as
   `last_error`; the run continues.

### Mirror pass with blob-stripping

Set `mirror.strip_blobs_larger_than_mb = N` in config to work around GitHub's
100 MB per-file push ceiling. Per affected repo, the flow becomes:

1. `git cat-file --batch-all-objects` on the source cache locates any blob
   larger than the threshold. If none, the standard push applies — no
   rewriting, no divergence.
2. If the repo contains any oversized blob, a sidecar mirror at
   `cache/<gitlab_project_id>.filtered.git/` is wiped and rebuilt from the
   source via a local `git clone --mirror`, then
   `git filter-repo --strip-blobs-bigger-than NM --force` runs against the
   sidecar.
3. `git push --mirror` pushes the sidecar to GitHub.

`git-filter-repo` is deterministic, so subsequent runs on an unchanged source
produce identical commit SHAs on the filtered side — the push after that is a
no-op at the protocol level. When new commits land on GitLab, the sidecar is
rebuilt and pushed.

Consequences:

- GitHub commit SHAs do **not** match GitLab for affected repos. `state.json`
  tracks per-project id not SHAs, so reconciliation is unaffected.
- The GitHub copy is a code-visibility artefact. Cloning from GitHub yields a
  history with the big blobs removed; cloning from GitLab still has them.
- `git-filter-repo` must be on PATH at runtime. Installed in the Docker image
  via apt; for non-Docker runs install it via your OS package manager
  (`emerge dev-vcs/git-filter-repo` on Gentoo,
  `apt-get install git-filter-repo` on Debian, `pip install git-filter-repo`
  into your venv) and it will be found automatically.

### Profile pass

1. `profile.stats.aggregate()` pulls the project list and computes per-language
   **lines of code** by extracting each project's ``HEAD`` tree from its bare
   cache via ``git archive`` and running ``cloc --json`` over it. Totals are
   summed across all projects (public + private — aggregates do not leak
   names). LOC is used instead of byte-weighting because the latter
   over-counts data-heavy repos (e.g., ``1brc-c`` with ~6 GB of measurement
   data gets correctly counted as its actual ~5 kLOC of C source). Totals are
   cached in ``state.profile.language_cache`` for 24 h.
2. The authenticated user's recent events are fetched (over-fetched 3×
   `recent_activity_count`) and filtered to events whose project is public
   or has no project. Private-project events are dropped to avoid leaking
   names.
3. Recently-updated public repos are selected by `last_activity_at`.
4. `profile.render.render()` builds Markdown from the aggregate. It is
   rendered *twice*: once without a disclaimer (for GitLab), once with the
   configured disclaimer as a block-quote above the bio (for GitHub).
5. Each rendering is SHA-256-hashed and compared against the last-published
   hash in state. If unchanged, publishing is skipped entirely. Otherwise
   the runner publishes via `PUT /projects/:id/repository/files/...` on
   GitLab (with `last_commit_id` CAS) and `PUT /repos/:owner/:repo/contents/...`
   on GitHub (with `sha` CAS).
6. The footer timestamp is *date-only* (`YYYY-MM-DD`), so daily runs with
   unchanged content hash identically. This is intentional — it prevents a
   no-op republish every day just to bump a second-resolution timestamp.

## Configuration schema

All non-secret config is in TOML. The default path is
`/etc/git-sync/config.toml`; override with `GIT_SYNC_CONFIG` or `--config`.

| Key                                  | Required | Default      | Notes |
| ------------------------------------ | -------- | ------------ | ----- |
| `gitlab.url`                         | yes      |              | Base URL of the GitLab host. Can also be set via `GITLAB_URL` env (env wins). |
| `github.owner`                       | yes      |              | GitHub account handle that owns the mirrors. Can also be set via `GITHUB_OWNER`. |
| `author.name`, `author.email`        | yes      |              | Commit author for profile README pushes. Email must be verified on both platforms for contribution graph attribution. |
| `mirror.enabled`                     | no       | `true`       | Reserved for future use; currently the `mirror` subcommand runs unconditionally. |
| `mirror.strip_blobs_larger_than_mb`  | no       | *unset*      | When set, repos with blobs over this threshold are rewritten via git-filter-repo before the GitHub push. See "Mirror pass with blob-stripping" above. |
| `mirror.exclude_groups`              | no       | `[]`         | List of GitLab group prefixes (first path segment) to skip entirely. Projects in these groups are never enumerated for mirror, and any existing GitHub mirror for such a project is left untouched (no orphan flip). |
| `mirror.mirror_private_repos`        | no       | `false`      | When true, private GitLab projects are mirrored to **private** GitHub repos (created if absent, flipped from public if needed). Useful for populating the GitHub contribution graph with private-work commits; commits preserve their original author and date, so attribution works even with blob-stripping active. |
| `mirror.only_group_owned`            | no       | `false`      | When true, skip any GitLab project whose `namespace.kind` is `"user"` (i.e. lives in the authenticated user's personal namespace rather than a group). Pairs well with `mirror_private_repos = true` when you want to mirror real work but keep scratch/personal repos out of the mirror. |
| `profile.enabled`                    | no       | `true`       | Reserved for future use. |
| `profile.top_n_languages`            | no       | `8`          | Top N languages shown in the table. |
| `profile.recent_activity_count`      | no       | `20`         | Events shown in the activity list (after public-project filter). |
| `profile.recent_repos_count`         | no       | `5`          | Public repos shown in the "Recently updated" table. Set to `0` to list every public repo. |
| `profile.gitlab_path`                | yes      |              | GitLab path of the profile repo, e.g. `vifair22/vifair22`. |
| `profile.github_repo`                | yes      |              | GitHub repo name for the profile README (owner comes from `github.owner`). |
| `profile.github_disclaimer`          | no       | `""`         | Multiline string block-quoted at the top of the GitHub copy only. |
| `paths.state`                        | yes      |              | Path to `state.json`. |
| `paths.cache`                        | yes      |              | Directory for mirror bare-clone caches. |
| `paths.about`                        | yes      |              | Path to `about.md`, read verbatim into the profile. |
| `schedule.mirror_interval_hours`     | no       | `24`         | Daemon interval between mirror passes. |
| `schedule.profile_interval_hours`    | no       | `24`         | Daemon interval between profile passes. |
| `logging.level`                      | no       | `INFO`       | Standard logging levels. |

### Environment variables

| Variable          | Required | Purpose                                   |
| ----------------- | -------- | ----------------------------------------- |
| `GITLAB_TOKEN`    | yes      | PAT with `api` + `read_repository`. |
| `GITHUB_TOKEN`    | yes      | Classic PAT with `repo` scope. |
| `GITLAB_URL`      | no       | Overrides `gitlab.url`. |
| `GITHUB_OWNER`    | no       | Overrides `github.owner`. |
| `GIT_SYNC_CONFIG` | no       | Overrides the default config path. |

## State file

JSON at `paths.state`. Structure:

```jsonc
{
  "repos": {
    "<gitlab_project_id>": {
      "gitlab_id": 42,
      "gitlab_path": "vifair22/foo",
      "github_name": "foo",
      "last_known_visibility": "public",   // "public" | "private"
      "last_sync_utc": "2026-04-20T12:00:00+00:00",
      "last_error": null
    }
  },
  "profile": {
    "last_gitlab_hash": "sha256 of last published gitlab README",
    "last_github_hash": "sha256 of last published github README",
    "last_publish_utc": "2026-04-20T12:00:00+00:00",
    "language_cache_utc": "2026-04-20T12:00:00+00:00",
    "language_cache": { "C": 123456, "Python": 7890 }
  }
}
```

Keyed by GitLab project ID (not name) so project renames on GitLab do not
lose mirror history. Writes are atomic (`tempfile` + `os.fsync` + `os.replace`).

## Operational recovery

- **Lost state**: deleting `state.json` is safe. The next mirror pass re-enumerates both sides, re-derives actions, and re-populates state. Repos that already exist on GitHub at the right visibility will result in no-op actions aside from a `push --mirror`. Language cache will be rebuilt on the next profile pass.
- **Lost cache**: deleting `/var/lib/git-sync/cache/*.git` forces a fresh `git clone --mirror` on the next run. Bandwidth spike, no data loss.
- **Undo a wrong mirror**: git-sync never deletes GitHub repos. If a mirror should be torn down, delete the GitHub repo manually. On the next run it will be re-created (public, mirrored) or left alone (private GitLab source), consistent with the policy.
- **Bad GitHub state**: because GitHub is disposable, the simplest recovery is to wipe the GitHub side and re-run. git-sync will create and mirror everything from scratch.

## Failure modes and symptoms

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `error: Required environment variable GITLAB_TOKEN is unset` on start | env not passed | Check compose `environment:` and `.env`. |
| `HTTP 401` from GitLab enumeration | PAT expired or missing scope | Rotate the token with `read_api read_repository`. |
| `HTTP 403` on GitHub PATCH | missing `repo` scope or classic PAT | Reissue with `repo`. |
| `GitError: … failed (exit 128)` during mirror | bad URL, missing `git` binary, or GitHub repo deleted between enumeration and push | Check logs (credentials are scrubbed), re-run — mirror is idempotent. |
| Mirror push succeeds, default branch doesn't update | empty repo, or missing `administration` permission | Verify PAT scope; push at least one ref to GitLab. |
| Profile never publishes | `content_hash` matches last published | Expected when nothing changed; force by touching `about.md`. |
| Daemon exits silently | SIGTERM/SIGINT received | Expected. Compose `restart: unless-stopped` covers unplanned exits. |

## Observability

- Logs: stdlib `logging` to stderr, configurable level. Each task logs start, completion, per-repo action, and errors with module-level loggers (`git_sync.mirror`, `git_sync.profile`, `git_sync.daemon`, `git_sync.http`, `git_sync.git`).
- No metrics endpoint yet. For monitoring, scrape the container logs for `ERROR` lines, or wrap the daemon with a watcher that alerts on non-zero exit codes. M9 may add a Prometheus exporter.

## Security considerations

- **Credentials never logged**. Clone/push URLs embed the PAT as `oauth2:<token>@host/...`; every URL argument to `git` is scrubbed with a regex before logging, and `stderr` from failed `git` invocations is passed through the same scrub before being included in `GitError` messages. Test `test_git_error_message_scrubs_tokens` pins this behaviour.
- **Push-mirror is destructive**. `git push --mirror` force-updates the GitHub side and prunes any refs not present on GitLab. git-sync does not implement a safeguard against this because *GitHub is treated as disposable*. Do not point git-sync at a GitHub owner account that contains repos you care about but aren't on GitLab.
- **Private repos are never created on GitHub**. A repo that is private on GitLab will never have a GitHub counterpart created by git-sync. If it once was public and a GitHub mirror exists, that mirror is *flipped to private* on the next run.
- **Profile commit identity** matches your personal email so the contribution graphs on both platforms credit you. Since the tool holds PATs with write access to both your profile repos, treat its host the same way you'd treat a machine with your SSH key on it.

## Docker / Unraid deployment notes

Container bind-mounts:
- `/etc/git-sync` (read-only) — holds `config.toml` and `about.md`.
- `/var/lib/git-sync` (read-write) — holds `state.json` and `cache/`. This one must be persistent; losing it triggers the full re-enumeration described in "Operational recovery" (safe, but wastes bandwidth).

Unraid specifics:
- Put both volumes under `/mnt/user/appdata/git-sync/` (or wherever your appdata lives).
- Create the container with "Host access to custom networks" if `gitlab.url` is on a user-defined bridge network only — otherwise the default `bridge` network resolves internet hosts fine.
- The `user: "1000:1000"` in compose matches the `UID`/`GID` baked into the image. If your appdata owner is different, pass matching `UID=…` and `GID=…` build args to `docker buildx build`.

## Versioning

`release_version` holds the semver (`0.1.0`). At container build time,
`scripts/stamp_version.py` writes `src/git_sync/_version.py` with the full
build-stamped version string (`0.1.0_YYYYMMDD.HHMM.release`). `git-sync --version`
prints that exact string.

Bumps: edit `release_version`, rebuild the image, the new version is
stamped automatically.

## Tests

140 unit tests, ≈96 % line coverage. Run locally with
`.venv/bin/pytest --cov=git_sync`. All tests are offline — the git-ops tests
use local bare repos over `file://` URLs, the HTTP tests use a threaded stdlib
`http.server` on an ephemeral port, and the runner tests use in-memory fakes.
