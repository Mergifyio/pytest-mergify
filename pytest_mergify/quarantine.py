import dataclasses
import os
import typing

import _pytest.nodes
import pytest
import requests

from pytest_mergify import utils


@dataclasses.dataclass
class Quarantine:
    api_ctxt: utils.APIContext
    quarantined_tests: typing.List[str] = dataclasses.field(
        init=False, default_factory=list
    )
    quarantine_used_by_tests: typing.Set[str] = dataclasses.field(
        init=False, default_factory=set
    )
    init_error_msg: typing.Optional[str] = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        try:
            owner, repository = self.api_ctxt.full_repository_name.split("/")
        except ValueError:
            self.init_error_msg = f"Repository name '{self.api_ctxt.full_repository_name}' has an unexpected format (expected 'owner/repository'), skipping CI Insights Quarantine setup"
            return

        url = f"{self.api_ctxt.url}/v1/ci/{owner}/repositories/{repository}/quarantines"

        try:
            quarantine_resp: requests.Response = requests.get(
                url,
                headers=self.api_ctxt.authorization_header(),
                params={"branch": self.api_ctxt.branch_name},
                timeout=10,
            )
        except requests.RequestException as exc:
            self.init_error_msg = f"Failed to connect to Mergify's API, tests won't be quarantined. Error: {str(exc)}"
            return

        if quarantine_resp.status_code == 402:
            # No CI Insights Quarantine subscription, skip it.
            return

        try:
            quarantine_resp.raise_for_status()
        except requests.HTTPError as exc:
            self.init_error_msg = f"Error when querying Mergify's API, tests won't be quarantined. Error: {str(exc)}"
            return

        self.quarantined_tests = [
            qtest["test_name"] for qtest in quarantine_resp.json()["quarantined_tests"]
        ]

    def __contains__(self, item: _pytest.nodes.Item) -> bool:
        return item.nodeid in self.quarantined_tests

    def quarantined_tests_report(self) -> str:
        report_str = f"""🛡️ Quarantine
- Repository: {self.api_ctxt.full_repository_name}
- Branch: {self.api_ctxt.branch_name}
- Quarantined tests fetched from API: {len(self.quarantined_tests)}
"""

        if self.quarantine_used_by_tests:
            report_str += f"""
- 🔒 Quarantined:
    · {f"{os.linesep}    · ".join(sorted(self.quarantine_used_by_tests))}
"""

        unused_quarantined_tests = (
            set(self.quarantined_tests) - self.quarantine_used_by_tests
        )
        if unused_quarantined_tests:
            report_str += f"""
- Unused quarantined tests:
    · {f"{os.linesep}    · ".join(sorted(unused_quarantined_tests))}
"""

        return report_str

    def mark_test_as_quarantined(self, test_item: _pytest.nodes.Item) -> None:
        test_item.add_marker(
            pytest.mark.xfail(
                reason="Test is quarantined from Mergify CI Insights",
                raises=None,
                run=True,
                strict=False,
            ),
            append=True,
        )
        self.quarantine_used_by_tests.add(test_item.nodeid)
