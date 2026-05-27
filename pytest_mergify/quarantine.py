import dataclasses
import os
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
    quarantine_used_by_tests: typing.Set[str] = dataclasses.field(
        init=False, default_factory=set
    )
    init_error_msg: typing.Optional[str] = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        try:
            owner, repository = self.repo_name.split("/")
        except ValueError:
            self.init_error_msg = f"Repository name '{self.repo_name}' has an unexpected format (expected 'owner/repository'), skipping Mergify Test Insights Quarantine initialization"
            return

        url: typing.Optional[str] = (
            f"{self.api_url}/v1/ci/{owner}/repositories/{repository}/quarantines"
        )
        # Filters and per_page go on the first request only; for subsequent
        # pages the URL comes verbatim from the RFC 5988 `next` link and
        # carries everything the server wants on the next call.
        params: typing.Optional[typing.Dict[str, typing.Any]] = {
            "branch": self.branch_name,
            "per_page": 100,
        }
        headers = {"Authorization": f"Bearer {self.token}"}
        quarantined_tests: typing.List[str] = []
        # Guard against a buggy server (or stale CI proxy) returning a `next`
        # link that loops back to a URL we have already fetched.
        seen_urls: typing.Set[str] = set()

        while url is not None:
            if url in seen_urls:
                self.init_error_msg = "Mergify's API returned a cyclic `next` link, aborting. Tests won't be quarantined."
                return
            seen_urls.add(url)

            try:
                quarantine_resp: requests.Response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=10,
                )
            except requests.RequestException as exc:
                self.init_error_msg = f"Failed to connect to Mergify's API, tests won't be quarantined. Error: {str(exc)}"
                return

            if quarantine_resp.status_code == 402:
                # No Mergify Test Insights Quarantine subscription, skip it.
                return

            try:
                quarantine_resp.raise_for_status()
            except requests.HTTPError as exc:
                self.init_error_msg = f"Error when querying Mergify's API, tests won't be quarantined. Error: {str(exc)}"
                return

            quarantined_tests.extend(
                qtest["test_name"]
                for qtest in quarantine_resp.json()["quarantined_tests"]
            )

            next_link = quarantine_resp.links.get("next")
            url = next_link["url"] if next_link else None
            params = None

        self.quarantined_tests = quarantined_tests

    def __contains__(self, item: _pytest.nodes.Item) -> bool:
        return item.nodeid in self.quarantined_tests

    def quarantined_tests_report(self) -> str:
        report_str = f"""🛡️ Quarantine
- Repository: {self.repo_name}
- Branch: {self.branch_name}
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
                reason="Test is quarantined from Mergify Test Insights",
                raises=None,
                run=True,
                strict=False,
            ),
            append=True,
        )
        self.quarantine_used_by_tests.add(test_item.nodeid)
