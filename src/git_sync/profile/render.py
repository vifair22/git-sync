"""Render a profile README in Markdown from a ``ProfileData`` object.

Pure, zero-dependency: no template engine. The structure is fixed (disclaimer,
bio, languages, recently updated, recent activity, footer) and the only
user-editable prose is the ``about.md`` snippet passed in as ``about_text``.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .. import VERSION
from .stats import LanguageStat, ProfileData

_GIT_SYNC_REPO_URL = "https://git.airies.net/vifair22/git-sync"


# Language → (simple-icons slug or None, hex color).
# Slug set to None means no logo; badge still rendered with name + color.
_LANG_META: dict[str, tuple[str | None, str]] = {
    "C":               ("c",              "A8B9CC"),
    "C++":             ("cplusplus",      "00599C"),
    "C/C++ Header":    ("c",              "A8B9CC"),
    "Objective-C":     ("apple",          "438EFF"),
    "C#":              ("sharp",          "239120"),
    "Python":          ("python",         "3776AB"),
    "TypeScript":      ("typescript",     "3178C6"),
    "JavaScript":      ("javascript",     "F7DF1E"),
    "TSX":             ("react",          "61DAFB"),
    "JSX":             ("react",          "61DAFB"),
    "Rust":            ("rust",           "DEA584"),
    "Go":              ("go",             "00ADD8"),
    "Zig":             ("zig",            "F7A41D"),
    "Bourne Shell":    ("gnubash",        "4EAA25"),
    "Shell":           ("gnubash",        "4EAA25"),
    "Bourne Again Shell": ("gnubash",     "4EAA25"),
    "PowerShell":      ("powershell",     "5391FE"),
    "Batchfile":       (None,             "C1F12E"),
    "Ruby":            ("ruby",           "CC342D"),
    "Java":            ("openjdk",        "007396"),
    "Kotlin":          ("kotlin",         "7F52FF"),
    "Swift":           ("swift",          "F05138"),
    "PHP":             ("php",            "777BB4"),
    "Perl":            ("perl",           "39457E"),
    "Lua":             ("lua",            "2C2D72"),
    "CSS":             ("css3",           "1572B6"),
    "HTML":            ("html5",          "E34F26"),
    "Vue":             ("vuedotjs",       "4FC08D"),
    "Svelte":          ("svelte",         "FF3E00"),
    "Scala":           ("scala",          "DC322F"),
    "R":               ("r",              "276DC3"),
    "Haskell":         ("haskell",        "5D4F85"),
    "Elixir":          ("elixir",         "4B275F"),
    "Erlang":          ("erlang",         "A90533"),
    "Clojure":         ("clojure",        "5881D8"),
    "OCaml":           ("ocaml",          "EC6813"),
    "Dart":            ("dart",           "0175C2"),
    "SQL":             ("postgresql",     "336791"),
    "Assembly":        (None,             "6E4C13"),
    "Makefile":        ("gnu",            "A42E2B"),
    "Dockerfile":      ("docker",         "2496ED"),
    "CMake":           ("cmake",          "064F8C"),
    "EJS":             (None,             "A91E50"),
    "Prolog":          (None,             "74283C"),
    "Verilog":         (None,             "B2B7F8"),
    "GLSL":            (None,             "5686A4"),
    "Tcl":             (None,             "E4CD4C"),
    "Pascal":          (None,             "E3F171"),
    "Limbo":           (None,             "808080"),
    "RPC":             (None,             "808080"),
}

_DEFAULT_COLOR = "808080"


def _language_badge(lang: LanguageStat) -> str:
    slug, color = _LANG_META.get(lang.name, (None, _DEFAULT_COLOR))
    params: dict[str, str] = {
        "label": lang.name,
        "message": f"{lang.pct:.1f}%",
        "color": color,
        "style": "flat-square",
    }
    if slug:
        params["logo"] = slug
        params["logoColor"] = "white"
    url = "https://img.shields.io/static/v1?" + urlencode(params)
    return f"![{lang.name}]({url})"


def _highlight_badge(language: str) -> str:
    slug, color = _LANG_META.get(language, (None, _DEFAULT_COLOR))
    params: dict[str, str] = {
        "label": "",
        "message": language,
        "color": color,
        "style": "flat-square",
    }
    if slug:
        params["logo"] = slug
        params["logoColor"] = "white"
    url = "https://img.shields.io/static/v1?" + urlencode(params)
    return f"![{language}]({url})"


def render(
    data: ProfileData,
    *,
    about_text: str,
    gitlab_base_url: str,
    disclaimer: str = "",
) -> str:
    lines: list[str] = []

    if disclaimer.strip():
        for line in disclaimer.strip().splitlines():
            lines.append(f"> {line}" if line else ">")
        lines.append("")

    bio = about_text.rstrip()
    if bio:
        lines.append(bio)
        lines.append("")

    if data.top_languages:
        lines.append("## Top languages")
        lines.append("")
        badges = [_language_badge(lang) for lang in data.top_languages]
        # Stack vertically: trailing two spaces force a markdown hard line
        # break so each badge sits on its own line instead of wrapping inline.
        for i, badge in enumerate(badges):
            suffix = "  " if i < len(badges) - 1 else ""
            lines.append(badge + suffix)
        lines.append("")
        total_loc = sum(lang.bytes for lang in data.top_languages)
        lines.append(
            f"<sub>Computed via cloc across my public + private repos. "
            f"Top {len(data.top_languages)} shown; "
            f"{total_loc:,} LOC in this slice.</sub>"
        )
        lines.append("")

    if data.highlights:
        lines.append("## Project highlights")
        lines.append("")
        base = gitlab_base_url.rstrip("/")
        for h in data.highlights:
            name = h.path.rsplit("/", 1)[-1]
            url = f"{base}/{h.path}"
            summary = h.summary.rstrip()
            # Trailing period looks odd right before "(stack)"; drop if present.
            if summary.endswith("."):
                summary = summary[:-1]
            lines.append(f"**[{name}]({url})** — {summary} ({h.stack}).")
            lines.append("")
    elif data.recent_repos:
        lines.append("## Recently updated")
        lines.append("")
        lines.append("| Repo | Updated |")
        lines.append("| ---- | ------- |")
        base = gitlab_base_url.rstrip("/")
        for repo in data.recent_repos:
            url = f"{base}/{repo.path_with_namespace}"
            lines.append(
                f"| [{repo.path_with_namespace}]({url}) | {repo.last_activity_at} |"
            )
        lines.append("")

    if data.recent_activity:
        lines.append("## Recent activity")
        lines.append("")
        for event in data.recent_activity:
            suffix = f": {event.target_title}" if event.target_title else ""
            lines.append(
                f"- {event.created_at} — {event.action_name}{suffix}"
            )
        lines.append("")

    lines.append("---")
    # Date-only (not full timestamp) so daily runs with unchanged content hash
    # identically; the content-hash gate in the runner then skips publishing.
    # VERSION is build-stamped and only changes on a new image build, so it
    # doesn't defeat the hash gate either.
    date = data.generated_at_utc[:10]
    lines.append(
        f"*Generated by [git-sync]({_GIT_SYNC_REPO_URL}) {VERSION} {date} · "
        f"{data.total_public_repos} public / {data.total_all_repos} total repos*"
    )

    return "\n".join(lines) + "\n"
