import dataclasses
import datetime
import os
import typing

import _pytest
import _pytest.main
import _pytest.nodes
import _pytest.reports
import requests

from pytest_mergify import utils

# NOTE(remyduthu): We are using a hard-coded budget for now, but the idea is to
# make it configurable in the future.
_DEFAULT_TEST_RETRY_BUDGET_RATIO = 0.1
_MAX_TEST_NAME_LENGTH = 65536
_MIN_TEST_RETRY_COUNT = 5
_MAX_TEST_RETRY_COUNT = 1000
_MIN_TEST_RETRY_BUDGET_DURATION = datetime.timedelta(seconds=4)


@dataclasses.dataclass
class FlakyDetector:
    token: str
    url: str
    full_repository_name: str
    branch_name: str

    last_collected_test: typing.Optional[str] = dataclasses.field(
        init=False, default=None
    )
    _deadline: typing.Optional[datetime.datetime] = dataclasses.field(
        init=False, default=None
    )
    _existing_tests: typing.List[str] = dataclasses.field(
        init=False,
        default_factory=list,
    )
    _new_test_durations: typing.Dict[str, datetime.timedelta] = dataclasses.field(
        init=False, default_factory=dict
    )
    _new_test_retries: typing.DefaultDict[str, int] = dataclasses.field(
        init=False, default_factory=lambda: typing.DefaultDict(int)
    )
    _over_length_tests: typing.Set[str] = dataclasses.field(
        init=False, default_factory=set
    )
    _total_test_durations: datetime.timedelta = dataclasses.field(
        init=False,
        default=datetime.timedelta(),
    )

    def __post_init__(self) -> None:
        self._existing_tests = self._fetch_existing_tests()

    def _fetch_existing_tests(self) -> typing.List[str]:
        owner, repository_name = utils.split_full_repo_name(
            self.full_repository_name,
        )

        response = requests.get(
            url=f"{self.url}/v1/ci/{owner}/tests/names",
            headers={"Authorization": f"Bearer {self.token}"},
            params={
                "repository": repository_name,
                "branch": self.branch_name,
            },
            timeout=10,
        )

        response.raise_for_status()

        result = typing.cast(typing.List[str], response.json()["test_names"])
        if len(result) == 0:
            raise RuntimeError(
                f"No existing tests found for '{self.full_repository_name}' repository on branch '{self.branch_name}'",
            )

        return result

    def detect_from_report(self, report: _pytest.reports.TestReport) -> bool:
        if report.when != "call":
            return False

        if report.outcome not in ["failed", "passed"]:
            return False

        duration = datetime.timedelta(seconds=report.duration)
        self._total_test_durations += duration

        test = report.nodeid
        if test in self._existing_tests:
            return False

        if test in self._new_test_durations:
            return True

        if len(test) > _MAX_TEST_NAME_LENGTH:
            self._over_length_tests.add(test)
            return False

        self._new_test_durations[test] = duration
        return True

    def _get_budget_deadline(self) -> datetime.datetime:
        return (
            datetime.datetime.now(datetime.timezone.utc) + self._get_budget_duration()
        )

    def _get_remaining_items(
        self,
        session: _pytest.main.Session,
    ) -> typing.List[_pytest.nodes.Item]:
        """
        Return the remaining items for this session based on the current state
        of the flaky detection. It can be called multiple times as we track
        already scheduled retries so we only return what's still needed.
        """

        # If we have exactly one new test and it's the last one, we can't know
        # its duration yet, so allocate max retries directly and rely on the
        # budget deadline instead of going through the budget allocation.
        if (
            len(self._new_test_durations) == 0
            and self.last_collected_test
            and self.last_collected_test not in self._existing_tests
        ):
            allocation = {self.last_collected_test: _MAX_TEST_RETRY_COUNT}
        else:
            allocation = _allocate_test_retries(
                self._get_budget_duration(),
                self._new_test_durations,
            )

        items_to_retry = [item for item in session.items if item.nodeid in allocation]

        result = []
        for item in items_to_retry:
            expected_retries = int(allocation[item.nodeid])
            existing_retries = int(
                self._new_test_retries.get(item.nodeid, 0),
            )

            remaining_retries = max(0, expected_retries - existing_retries)
            for _ in range(remaining_retries):
                # The parent is a class or a module in our case. It should
                # always be defined, but we handle it gracefully just in case.
                if not item.parent:
                    continue

                self._new_test_retries[item.nodeid] += 1

                clone = item.__class__.from_parent(
                    name=item.name,
                    parent=item.parent,
                )
                result.append(clone)

        # Manually trigger pytest hooks for the new items. This ensures plugins
        # like `pytest-asyncio` process them.
        session.config.hook.pytest_collection_modifyitems(
            session=session,
            config=session.config,
            items=result,
        )

        return result

    def handle_item(
        self,
        item: _pytest.nodes.Item,
        nextitem: typing.Optional[_pytest.nodes.Item],
    ) -> None:
        if self._deadline:
            if datetime.datetime.now(datetime.timezone.utc) > self._deadline:
                # Hard deadline to protect the budget allocated to flaky
                # detection. If tests take longer than expected, we stop
                # immediately.
                item.session.items.clear()

            return

        # Before we run the last item, append the items generated by the flaky
        # detector so this test's teardown still sees a next test. Otherwise
        # pytest considers the session finished, tears down session-scoped
        # fixtures, and our end-of-queue retries won't run.
        is_just_before_last_item = (
            self.last_collected_test
            and nextitem
            and self.last_collected_test == nextitem.nodeid
        )

        # After we run the last item, we know how long it took. If that unlocks
        # more budget for the flaky detection, use it now.
        is_last_item = (
            self.last_collected_test and self.last_collected_test == item.nodeid
        )
        if not (is_just_before_last_item or is_last_item):
            return

        item.session.items.extend(self._get_remaining_items(item.session))

        if is_last_item:
            self._deadline = self._get_budget_deadline()

    def make_report(self) -> str:
        result = "🐛 Flaky detection"
        if self._over_length_tests:
            result += f"{os.linesep}- Skipped {len(self._over_length_tests)} test(s):"
            for test in self._over_length_tests:
                result += (
                    f"{os.linesep}    • '{test}' has not been tested multiple "
                    f"times because the name of the test exceeds our limit of "
                    f"{_MAX_TEST_NAME_LENGTH} characters"
                )

        if not self._new_test_durations:
            result += f"{os.linesep}- No new tests detected, but we are watching 👀"

            return result

        total_retry_duration_seconds = sum(
            self._new_test_durations[test_name].total_seconds() * retry_count
            for test_name, retry_count in self._new_test_retries.items()
            if retry_count > 0
        )
        budget_duration_seconds = self._get_budget_duration().total_seconds()
        result += (
            f"{os.linesep}- Used {total_retry_duration_seconds / budget_duration_seconds * 100:.2f} % "
            f"of the budget ({total_retry_duration_seconds:.2f} s/{budget_duration_seconds:.2f} s)"
        )

        result += (
            f"{os.linesep}- Active for {len(self._new_test_durations)} new test(s):"
        )
        for test, duration in self._new_test_durations.items():
            retry_count = self._new_test_retries.get(test, 0)
            if retry_count == 0:
                result += f"{os.linesep}    • '{test}' is too slow to be tested at least {_MIN_TEST_RETRY_COUNT} times within the budget"
                continue
            elif retry_count < _MIN_TEST_RETRY_COUNT:
                result += f"{os.linesep}    • '{test}' has been tested only {retry_count} times to avoid exceeding the budget"
                continue

            retry_duration_seconds = duration.total_seconds() * retry_count

            result += (
                f"{os.linesep}    • '{test}' has been tested {retry_count} "
                f"times using approx. {retry_duration_seconds / budget_duration_seconds * 100:.2f} % "
                f"of the budget ({retry_duration_seconds:.2f} s/{budget_duration_seconds:.2f} s)"
            )

        return result

    def _get_budget_duration(self) -> datetime.timedelta:
        """
        Calculate the budget duration based on a percentage of total test
        execution time.

        The budget ensures there's always a minimum time allocated of
        '_MIN_TEST_RETRY_BUDGET_DURATION' even for very short test suites,
        preventing overly restrictive retry policies when the total test
        duration is small.
        """
        return max(
            _DEFAULT_TEST_RETRY_BUDGET_RATIO * self._total_test_durations,
            _MIN_TEST_RETRY_BUDGET_DURATION,
        )


def _select_affordable_tests(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
) -> typing.Dict[str, datetime.timedelta]:
    """
    Select tests that can be retried within the given budget.

    This ensures we don't select tests that would exceed our time constraints
    even with the minimum number of retries.
    """
    if len(test_durations) == 0:
        return {}

    budget_per_test = budget_duration / len(test_durations)

    result = {}
    for test, duration in test_durations.items():
        expected_retries_duration = duration * _MIN_TEST_RETRY_COUNT
        if expected_retries_duration <= budget_per_test:
            result[test] = duration

    return result


def _allocate_test_retries(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
) -> typing.Dict[str, int]:
    """
    Distribute retries within a fixed time budget.

    Why this shape:

    1. First, drop tests that aren't affordable (cannot reach
    `_MIN_TEST_RETRY_COUNT` within the budget). This avoids wasting time on
    tests that would starve the rest.

    2. Then allocate from fastest to slowest to free budget early: fast tests
    often hit `_MAX_TEST_RETRY_COUNT`; when capped, leftover time rolls over to
    slower tests.

    3. At each step we recompute a fair per-test slice from the remaining budget
    and remaining tests, so the distribution adapts as we go.
    """

    allocation: typing.Dict[str, int] = {}

    affordable_test_durations = _select_affordable_tests(
        budget_duration,
        test_durations,
    )

    for test, duration in sorted(
        affordable_test_durations.items(),
        key=lambda item: item[1],
    ):
        remaining_budget = budget_duration - sum(
            (allocation[t] * affordable_test_durations[t] for t in allocation),
            start=datetime.timedelta(),
        )
        remaining_test_count = len(affordable_test_durations) - len(allocation)

        budget_per_test = remaining_budget / remaining_test_count

        # Guard against zero or negative duration to prevent division by zero.
        # If a test reports a zero duration, it means it's effectively free to
        # retry, so we assign the maximum allowed retries within our global cap.
        if duration <= datetime.timedelta():
            allocation[test] = _MAX_TEST_RETRY_COUNT
            continue

        allocation[test] = min(budget_per_test // duration, _MAX_TEST_RETRY_COUNT)

    return allocation
