import dataclasses
import typing

import _pytest.config
import _pytest.nodes
import requests


@dataclasses.dataclass
class TestSelection:
    """Ask Mergify whether this run should execute only a subset of tests.

    A merge-queue rerun (a `max_checks_retries` attempt or a bisection step)
    only needs to replay the tests that failed on the previous attempt.
    Mergify resolves that server-side from the run's own identity (queue
    branch + head SHA + job); the plugin sends what it already knows and
    applies the answer to the collection. Every error, timeout, or unknown
    situation degrades to running the full suite — this feature can only
    remove work, never correctness.
    """

    api_url: str
    token: str
    repo_name: str
    branch_name: str
    head_sha: str
    pipeline_name: str
    job_name: str

    selection: typing.Literal["full", "subset"] = dataclasses.field(
        init=False, default="full"
    )
    reason: str = dataclasses.field(init=False, default="not_requested")
    tests: typing.List[str] = dataclasses.field(init=False, default_factory=list)
    init_error_msg: typing.Optional[str] = dataclasses.field(init=False, default=None)
    kept_count: typing.Optional[int] = dataclasses.field(init=False, default=None)
    deselected_count: int = dataclasses.field(init=False, default=0)

    def __post_init__(self) -> None:
        try:
            owner, repository = self.repo_name.split("/")
        except ValueError:
            self.init_error_msg = f"Repository name '{self.repo_name}' has an unexpected format (expected 'owner/repository'), skipping Mergify test selection"
            return

        try:
            response = requests.get(
                f"{self.api_url}/v1/ci/{owner}/repositories/{repository}/test-selection",
                headers={"Authorization": f"Bearer {self.token}"},
                params={
                    "branch": self.branch_name,
                    "head_sha": self.head_sha,
                    "pipeline_name": self.pipeline_name,
                    "job_name": self.job_name,
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            self.init_error_msg = f"Failed to connect to Mergify's API, the full test suite will run. Error: {str(exc)}"
            return

        if response.status_code in (402, 404):
            # No subscription, or an engine without the endpoint: silently
            # run the full suite.
            return

        try:
            response.raise_for_status()
            payload = response.json()
            selection = payload["selection"]
            reason = payload["reason"]
            # The response is polymorphic on `selection`: `tests` is part of a
            # `subset` answer and absent from a `full` one. Reading it only for
            # a subset mirrors that, and keeps the KeyError below meaningful --
            # a subset with no `tests` is a real protocol break worth surfacing,
            # which a blanket `.get("tests", [])` would silently run past.
            tests = payload["tests"] if selection == "subset" else []
        except (
            requests.HTTPError,
            requests.exceptions.JSONDecodeError,
            KeyError,
            # A payload that is valid JSON but not an object (list, string,
            # number...) raises TypeError on subscript access.
            TypeError,
        ) as exc:
            self.init_error_msg = f"Error when querying Mergify's API, the full test suite will run. Error: {str(exc)}"
            return

        if selection == "subset" and tests:
            self.selection = "subset"
            self.tests = tests
        self.reason = str(reason)

    def filter_items(
        self,
        config: _pytest.config.Config,
        items: typing.List[_pytest.nodes.Item],
    ) -> None:
        """Reduce the collected items to the served subset, in place.

        Matching is by exact nodeid — the identifiers Mergify serves are the
        ones this plugin previously uploaded. Served names absent from the
        collection are ignored; if NOTHING matches (e.g. the tests were
        renamed since the previous attempt), the full suite runs — an empty
        reduced run would turn green without testing anything.
        """
        if self.selection != "subset":
            return

        subset = set(self.tests)
        kept = [item for item in items if item.nodeid in subset]
        if not kept:
            self.selection = "full"
            self.reason = "subset_matched_no_collected_test"
            return

        deselected = [item for item in items if item.nodeid not in subset]
        if deselected:
            items[:] = kept
            config.hook.pytest_deselected(items=deselected)

        self.kept_count = len(kept)
        self.deselected_count = len(deselected)

    def report(self) -> str:
        report_str = f"""✂️ Test selection
- Selection: {self.selection} (reason: {self.reason})
"""
        if self.selection == "subset" and self.kept_count is not None:
            report_str += f"- Reduced rerun: executing {self.kept_count} previously-failing test(s), {self.deselected_count} deselected\n"
        return report_str
