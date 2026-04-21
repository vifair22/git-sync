"""Tests for the CLI entry point."""
from __future__ import annotations

import pytest

from git_sync import VERSION, cli, config
from git_sync.mirror.runner import MirrorResult
from git_sync.profile.runner import ProfileResult


_BASE_TOML = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[author]
name = "Alice"
email = "alice@example.com"

[profile]
gitlab_path = "alice/alice"
github_repo = "alice"

[paths]
state = "{state}"
cache = "{cache}"
about = "{about}"
"""


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    p = tmp_path / "config.toml"
    p.write_text(
        _BASE_TOML.format(
            state=tmp_path / "state.json",
            cache=tmp_path / "cache",
            about=tmp_path / "about.md",
        )
    )
    return p


def test_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert VERSION in (captured.out + captured.err)


def test_no_subcommand_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code != 0


def test_mirror_subcommand_dispatches(cfg_path, monkeypatch):
    calls: dict = {}

    def fake(cfg, *, dry_run):
        calls["cfg"] = cfg
        calls["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(cli, "_cmd_mirror", fake)
    assert cli.main(["--config", str(cfg_path), "--dry-run", "mirror"]) == 0
    assert calls["dry_run"] is True


def test_profile_subcommand_dispatches(cfg_path, monkeypatch):
    calls: dict = {}

    def fake(cfg, *, dry_run, refresh_languages=False):
        calls["dry_run"] = dry_run
        calls["refresh"] = refresh_languages
        return 0

    monkeypatch.setattr(cli, "_cmd_profile", fake)
    assert cli.main(["--config", str(cfg_path), "profile"]) == 0
    assert calls["dry_run"] is False
    assert calls["refresh"] is False


def test_run_subcommand_dispatches(cfg_path, monkeypatch):
    monkeypatch.setattr(cli, "_cmd_run", lambda cfg, *, dry_run: 0)
    assert cli.main(["--config", str(cfg_path), "run"]) == 0


def test_missing_config_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    missing = tmp_path / "nope.toml"
    assert cli.main(["--config", str(missing), "mirror"]) == 2
    assert "not found" in capsys.readouterr().err


def test_cmd_mirror_calls_runner_and_returns_zero(cfg_path, monkeypatch):
    instances: list[dict] = []

    class FakeRunner:
        def __init__(self, **kwargs):
            instances.append(kwargs)

        def run(self, *, dry_run):
            return MirrorResult(successes=2, failures=0, skipped=1)

    monkeypatch.setattr(
        "git_sync.mirror.runner.MirrorRunner", FakeRunner,
    )
    cfg = config.load(cfg_path)

    rc = cli._cmd_mirror(cfg, dry_run=False)

    assert rc == 0
    assert instances[0]["github_owner"] == "alice"
    assert instances[0]["gitlab_url"] == "https://git.example.com"
    assert cfg.paths.state.exists()


def test_cmd_mirror_returns_1_on_failures(cfg_path, monkeypatch):
    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, *, dry_run):
            return MirrorResult(successes=0, failures=1, skipped=0)

    monkeypatch.setattr(
        "git_sync.mirror.runner.MirrorRunner", FakeRunner,
    )
    cfg = config.load(cfg_path)
    assert cli._cmd_mirror(cfg, dry_run=False) == 1


def test_cmd_mirror_dry_run_does_not_save_state(cfg_path, monkeypatch):
    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, *, dry_run):
            return MirrorResult(successes=0, failures=0, skipped=0)

    monkeypatch.setattr(
        "git_sync.mirror.runner.MirrorRunner", FakeRunner,
    )
    cfg = config.load(cfg_path)
    cli._cmd_mirror(cfg, dry_run=True)
    assert not cfg.paths.state.exists()


def test_cmd_profile_publishes_and_saves_state(cfg_path, monkeypatch):
    instances: list[dict] = []

    class FakeRunner:
        def __init__(self, **kwargs):
            instances.append(kwargs)

        def run(self, *, dry_run):
            return ProfileResult(published_gitlab=True, published_github=True)

    monkeypatch.setattr("git_sync.profile.runner.ProfileRunner", FakeRunner)
    cfg = config.load(cfg_path)
    (tmp := cfg.paths.about).parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("bio")

    rc = cli._cmd_profile(cfg, dry_run=False)

    assert rc == 0
    assert instances[0]["author_name"] == "Alice"
    assert instances[0]["gitlab_profile_path"] == "alice/alice"
    assert instances[0]["github_profile_repo"] == "alice"
    assert cfg.paths.state.exists()


def test_cmd_profile_returns_1_on_failure(cfg_path, monkeypatch):
    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, *, dry_run):
            return ProfileResult(failed_gitlab=True)

    monkeypatch.setattr("git_sync.profile.runner.ProfileRunner", FakeRunner)
    cfg = config.load(cfg_path)
    assert cli._cmd_profile(cfg, dry_run=False) == 1


def test_cmd_profile_refresh_languages_clears_cache(cfg_path, monkeypatch):
    captured: dict = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured["state"] = kwargs["state"]

        def run(self, *, dry_run):
            return ProfileResult()

    monkeypatch.setattr("git_sync.profile.runner.ProfileRunner", FakeRunner)
    cfg = config.load(cfg_path)

    # Seed cache on disk so we can verify it's cleared before runner.run() sees it.
    from git_sync import state as state_mod
    seeded = state_mod.State()
    seeded.profile.language_cache = {"C": 12345}
    seeded.profile.language_cache_utc = "2026-04-19T12:00:00+00:00"
    state_mod.save(cfg.paths.state, seeded)

    cli._cmd_profile(cfg, dry_run=False, refresh_languages=True)

    # The State passed to the runner should have an empty cache now.
    assert captured["state"].profile.language_cache == {}
    assert captured["state"].profile.language_cache_utc is None


def test_profile_subcommand_parses_refresh_languages(cfg_path, monkeypatch):
    captured: dict = {}

    def fake(cfg, *, dry_run, refresh_languages=False):
        captured["refresh"] = refresh_languages
        return 0

    monkeypatch.setattr(cli, "_cmd_profile", fake)
    cli.main(["--config", str(cfg_path), "profile", "--refresh-languages"])
    assert captured["refresh"] is True


def test_cmd_profile_dry_run_does_not_save(cfg_path, monkeypatch):
    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, *, dry_run):
            return ProfileResult()

    monkeypatch.setattr("git_sync.profile.runner.ProfileRunner", FakeRunner)
    cfg = config.load(cfg_path)
    cli._cmd_profile(cfg, dry_run=True)
    assert not cfg.paths.state.exists()


def test_cmd_run_dry_run_calls_each_task_once(cfg_path, monkeypatch):
    called = {"mirror": 0, "profile": 0}

    def fake_mirror(c, *, dry_run):
        called["mirror"] += 1
        assert dry_run is True
        return 0

    def fake_profile(c, *, dry_run):
        called["profile"] += 1
        assert dry_run is True
        return 0

    monkeypatch.setattr(cli, "_cmd_mirror", fake_mirror)
    monkeypatch.setattr(cli, "_cmd_profile", fake_profile)
    cfg = config.load(cfg_path)

    assert cli._cmd_run(cfg, dry_run=True) == 0
    assert called == {"mirror": 1, "profile": 1}


def test_cmd_run_daemon_invokes_loop_with_tasks(cfg_path, monkeypatch):
    captured: dict = {}

    def fake_run_loop(tasks, *, stop_flag=None, **_):
        captured["tasks"] = list(tasks)

    monkeypatch.setattr("git_sync.daemon.run_loop", fake_run_loop)
    monkeypatch.setattr("git_sync.daemon.SignalStopFlag", lambda: (lambda: True))
    cfg = config.load(cfg_path)

    assert cli._cmd_run(cfg, dry_run=False) == 0
    names = [t.name for t in captured["tasks"]]
    assert names == ["mirror", "profile"]


def test_missing_token_returns_2(cfg_path, monkeypatch, capsys):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    assert cli.main(["--config", str(cfg_path), "mirror"]) == 2
    assert "GITLAB_TOKEN" in capsys.readouterr().err
