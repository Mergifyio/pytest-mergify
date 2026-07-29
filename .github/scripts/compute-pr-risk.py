#!/usr/bin/env python3
"""Deterministic low-risk detection for pull requests.

Decides whether a pull request is safe enough to carry the "low risk"
label, which lets it merge on a single approval instead of two.

Ported from the monorepo without its heuristic scoring and LLM passes:
this repository is a single package, so the structural metrics those
passes correct for (cross-cutting directories, change entropy, author
familiarity) carry no signal here.

Reads from environment:
    BASE_SHA, HEAD_SHA, PR_TITLE
    GITHUB_OUTPUT, RUNNER_TEMP (set by GitHub Actions)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import subprocess


LOG = logging.getLogger(__name__)


def git(*args: str, timeout: int = 30) -> str:
    """Run a git command and return stripped stdout.

    Raises on a nonzero exit so the scorer fails closed: a git error that
    yielded empty stdout would otherwise read as an empty diff, and an
    empty diff scores as nothing to flag.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout.strip()


def changed_files(base_sha: str, head_sha: str) -> tuple[list[str], int]:
    """Return the changed paths and the total count of changed lines."""
    # Rename detection collapses a move into a single `{old => new}` path
    # that no prefix test can match, which would hide a workflow moved out
    # of .github/workflows from the CI security check. Plain add/delete
    # pairs keep every path test honest.
    numstat = git("diff", "--numstat", "--no-renames", f"{base_sha}...{head_sha}")
    paths: list[str] = []
    total_lines = 0
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        # Binary files report "-" instead of a line count.
        added = int(parts[0]) if parts[0] != "-" else 0
        deleted = int(parts[1]) if parts[1] != "-" else 0
        paths.append(parts[2])
        total_lines += added + deleted
    return paths, total_lines


# -- Fast-path gates --

# Conventional Commit types whose changes carry little behavioral risk as
# long as they stay small and away from sensitive areas.
SAFE_COMMIT_TYPES = frozenset(
    {"chore", "ci", "docs", "refactor", "style", "test"},
)

# Path fragments that keep a change off the fast-path: an edit here can
# reach the label mechanism itself, so it must never grant its own
# shortcut. Workflow files are judged by content, not path — see
# diff_touches_ci_security — so a benign env or timeout tweak stays
# eligible.
SENSITIVE_PATH_FRAGMENTS = (".github/scripts/", ".mergify")

# A workflow edit earns the fast-path only when every changed line is
# provably innocuous. This is an allowlist, not a denylist: a denylist of
# dangerous keywords misses anything edited under an unchanged key — a
# shell line appended to an existing `run:` block, a permission flipped
# read->write — because the dangerous keyword never appears on the changed
# line. A line is safe only when it is blank, a comment, a YAML document
# marker, or a single `key: value` mapping whose key is not
# security-relevant and whose value interpolates no `${{ }}` expression.
_CI_MAPPING_RE = re.compile(r"^-?\s*(?P<key>[\w.-]+)\s*:(?:\s|$)")

# Keys whose edit can change what CI runs, as whom, or on whose behalf:
# untrusted-input triggers, execution surface, checkout redirection,
# gating, runner selection, and secret/token plumbing — plus the
# GITHUB_TOKEN permission scopes, which are leaf keys under `permissions:`
# and so evade any `permissions:`-only match.
DANGEROUS_CI_KEYS = frozenset(
    {
        # triggers that expose the workflow to untrusted input
        "pull_request_target",
        "workflow_run",
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
        "discussion",
        "discussion_comment",
        "workflow_call",
        "workflow_dispatch",
        "repository_dispatch",
        # execution surface
        "run",
        "uses",
        "shell",
        "container",
        "image",
        "services",
        "entrypoint",
        "args",
        "runs-on",
        # checkout inputs that redirect which code a step runs; these are
        # leaf keys under an action's `with:` block, so an edit to one
        # never re-adds the `with:` line and would otherwise evade the
        # `with:`-only match
        "ref",
        "repository",
        # gating and identity
        "if",
        "needs",
        "environment",
        "with",
        "continue-on-error",
        "defaults",
        # secret / token plumbing
        "permissions",
        "secrets",
        "token",
        "github_token",
        # GITHUB_TOKEN permission scopes
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    },
)

SMALL_CHANGE_MAX_LINES = 100
SMALL_CHANGE_MAX_FILES = 10


def commit_type(pr_title: str) -> str | None:
    """Extract the Conventional Commit type from a pull request title.

    Returns None when the title is not in Conventional Commit form so a
    plain sentence starting with a safe word cannot trigger the fast-path.
    """
    match = re.match(r"^(\w+)(?:\([^)]*\))?!?:", pr_title.strip())
    return match.group(1).lower() if match else None


def _is_breaking_change(pr_title: str) -> bool:
    """Whether the title carries the Conventional Commit breaking marker."""
    return bool(re.match(r"^\w+(\([^)]*\))?!:", pr_title.strip()))


def _is_sensitive(path: str) -> bool:
    """Whether a changed file sits in an area that bars the fast-path."""
    return any(fragment in path for fragment in SENSITIVE_PATH_FRAGMENTS)


def _ci_line_is_safe(content: str) -> bool:
    """Whether a single changed workflow line is provably innocuous."""
    stripped = content.strip()
    if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
        return True
    # `${{ }}` interpolation reaches untrusted context (pull request title,
    # head ref, secrets) and is the injection surface, so no interpolated
    # line is safe.
    if "${{" in stripped:
        return False
    match = _CI_MAPPING_RE.match(stripped)
    # A bare shell line (no `key:`) is unprovable, so it is not safe.
    return match is not None and match.group("key").lower() not in DANGEROUS_CI_KEYS


def diff_touches_ci_security(diff_text: str) -> bool:
    """Whether a workflow diff changes anything beyond innocuous knobs.

    Only added and removed lines are inspected, so unchanged context — a
    surrounding `run:` step, an untouched `uses:` pin — never taints an
    otherwise trivial env-var, timeout, or matrix edit. Returns True (route
    to review) the moment one changed line is not provably safe.
    """
    for line in diff_text.splitlines():
        # File headers are `+++ `/`--- ` (space-delimited); an added or
        # removed content line beginning with `++`/`--` must still be
        # inspected.
        if not line or line[0] not in "+-" or line.startswith(("+++ ", "--- ")):
            continue
        if not _ci_line_is_safe(line[1:]):
            return True
    return False


def ci_change_is_dangerous(
    base_sha: str,
    head_sha: str,
    *,
    has_ci: bool,
) -> bool:
    """Whether the workflow diff touches CI's security surface.

    Diffs the whole workflows tree rather than per-file pathspecs so a
    rename (whose `{old => new}` numstat path would match nothing) cannot
    smuggle a dangerous edit past the check.
    """
    if not has_ci:
        return False
    diff = git("diff", f"{base_sha}...{head_sha}", "--", ".github/workflows/")
    return diff_touches_ci_security(diff)


def low_risk_blockers(
    paths: list[str],
    pr_title: str,
    total_lines: int,
    *,
    dangerous_ci: bool,
) -> list[str]:
    """Reasons a change cannot take the deterministic low-risk fast-path.

    An empty list means the change is small, safely typed and clear of
    sensitive files, so it earns the label without any reviewer input. A
    workflow edit qualifies only when it leaves CI's security surface
    (triggers, permissions, secrets, action pins, run steps) untouched.
    """
    blockers = []

    if _is_breaking_change(pr_title):
        blockers.append("Title marks a breaking change")

    change_type = commit_type(pr_title)
    if change_type not in SAFE_COMMIT_TYPES:
        safe = ", ".join(f"`{t}`" for t in sorted(SAFE_COMMIT_TYPES))
        blockers.append(
            f"Change type is `{change_type or 'unset'}`, not one of {safe}",
        )

    if total_lines > SMALL_CHANGE_MAX_LINES:
        blockers.append(
            f"Diff is {total_lines} lines, over the {SMALL_CHANGE_MAX_LINES} limit",
        )

    if len(paths) > SMALL_CHANGE_MAX_FILES:
        blockers.append(
            f"{len(paths)} files changed, over the {SMALL_CHANGE_MAX_FILES} limit",
        )

    if dangerous_ci:
        blockers.append("Workflow edit touches CI's security surface")

    sensitive = sorted(path for path in paths if _is_sensitive(path))
    if sensitive:
        blockers.append(f"Touches sensitive files: {', '.join(sensitive)}")

    return blockers


# -- Comment formatting --


def format_comment(
    blockers: list[str],
    *,
    change_type: str | None,
    files_changed: int,
    total_lines: int,
) -> str:
    """Format the verdict as a GitHub pull request comment."""
    lines = ["<!-- pr-risk-assessment -->", "## PR Risk Assessment", ""]

    if blockers:
        lines += [
            ":white_circle: **Not auto-labeled** — the usual two approvals apply.",
            "",
        ]
        lines += [f"- :mag: {blocker}" for blocker in blockers]
    else:
        lines += [
            ":green_circle: **Low risk** — labeled `low risk`, "
            "so one approval is enough.",
            "",
            f"- :white_check_mark: Safe change type (`{change_type}`)",
            f"- :white_check_mark: {total_lines} lines across "
            f"{files_changed} file(s), clear of sensitive paths",
            "",
            "<!-- auto-labeled: low-risk -->",
        ]

    lines += ["", "*A reviewer can add or remove the label at any time.*"]
    return "\n".join(lines)


# -- Main --


def main() -> None:
    base = os.environ["BASE_SHA"]
    head = os.environ["HEAD_SHA"]
    title = os.environ["PR_TITLE"]

    paths, total_lines = changed_files(base, head)
    if not paths:
        LOG.info("No files changed, skipping")
        return

    blockers = low_risk_blockers(
        paths,
        title,
        total_lines,
        dangerous_ci=ci_change_is_dangerous(
            base,
            head,
            has_ci=any(path.startswith(".github/workflows/") for path in paths),
        ),
    )
    auto_label = not blockers

    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with Path(output_file).open("a", encoding="utf-8") as f:
            f.write(f"auto_label={str(auto_label).lower()}\n")

    # Written to a temp file for the workflow to post; the comment body is
    # too large and too markdown-heavy to pass through a step output.
    temp = os.environ.get("RUNNER_TEMP", "/tmp")  # noqa: S108
    comment = format_comment(
        blockers,
        change_type=commit_type(title),
        files_changed=len(paths),
        total_lines=total_lines,
    )
    (Path(temp) / "risk-comment.md").write_text(comment, encoding="utf-8")

    LOG.info("auto_label=%s blockers=%s", auto_label, blockers)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
