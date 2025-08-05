import dataclasses
import pytest
import _pytest.nodes
import requests
import typing


@dataclasses.dataclass
class Quarantine:
    api_url: str
    token: str
    repo_name: str
    branch_name: str
    quarantined_tests: typing.List[str] = dataclasses.field(
        init=False, default_factory=list
    )
    init_error_msg: typing.Optional[str] = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        try:
            owner, repository = self.repo_name.split("/")
        except ValueError:
            self.init_error_msg = f"Repository name '{self.repo_name}' has an unexpected format (expected 'owner/repository'), skipping CI Insights Quarantine setup"
            return

        url = f"{self.api_url}/v1/ci/{owner}/repositories/{repository}/quarantines"

        try:
            quarantine_resp: requests.Response = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                params={"branch": self.branch_name},
                timeout=10,
            )
        except requests.ConnectionError as exc:
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

        self.quarantined_tests = quarantine_resp.json()["quarantined_tests"]

    def __contains__(self, item: _pytest.nodes.Item) -> bool:
        return item.nodeid in self.quarantined_tests

    @staticmethod
    def mark_test_as_quarantined(test_item: _pytest.nodes.Item) -> None:
        test_item.add_marker(
            pytest.mark.xfail(
                reason="Test is quarantined from Mergify CI Insights",
                raises=None,
                run=True,
                strict=False,
            ),
            append=True,
        )
