import collections
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


@dataclasses.dataclass
class _FlakyDetectionContext:
    budget_ratio: float
    existing_test_names: typing.List[str]
    existing_tests_mean_duration_ms: int
    max_test_execution_count: int
    max_test_name_length: int
    min_budget_duration_ms: int
    min_test_execution_count: int

    @property
    def existing_tests_mean_duration(self) -> datetime.timedelta:
        return datetime.timedelta(milliseconds=self.existing_tests_mean_duration_ms)

    @property
    def min_budget_duration(self) -> datetime.timedelta:
        return datetime.timedelta(milliseconds=self.min_budget_duration_ms)


@dataclasses.dataclass
class _NewTestMetrics:
    "Represents metrics collected for a new test."

    initial_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )
    "Represents the duration of the initial execution of the test."

    retry_count: int = dataclasses.field(default=0)
    "Represents the number of times the test has been retried so far."

    scheduled_retry_count: int = dataclasses.field(default=0)
    "Represents the number of retries that have been scheduled for this test depending on the budget."

    total_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )
    "Represents the total duration spent executing this test, including retries."

    def add_duration(self, duration: datetime.timedelta) -> None:
        if not self.initial_duration:
            self.initial_duration = duration

        self.retry_count += 1
        self.total_duration += duration


@dataclasses.dataclass
class FlakyDetector:
    token: str
    url: str
    full_repository_name: str

    last_collected_test: typing.Optional[str] = dataclasses.field(
        init=False, default=None
    )
    _context: _FlakyDetectionContext = dataclasses.field(init=False)
    _deadline: typing.Optional[datetime.datetime] = dataclasses.field(
        init=False, default=None
    )
    _new_test_metrics: typing.Dict[str, _NewTestMetrics] = dataclasses.field(
        init=False, default_factory=lambda: collections.defaultdict(_NewTestMetrics)
    )
    _over_length_tests: typing.Set[str] = dataclasses.field(
        init=False, default_factory=set
    )
    _total_test_durations: datetime.timedelta = dataclasses.field(
        init=False,
        default=datetime.timedelta(),
    )

    def __post_init__(self) -> None:
        self._context = self._fetch_context()

    def _fetch_context(self) -> _FlakyDetectionContext:
        owner, repository_name = utils.split_full_repo_name(
            self.full_repository_name,
        )

        response = requests.get(
            url=f"{self.url}/v1/ci/{owner}/repositories/{repository_name}/flaky-detection-context",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
        )

        response.raise_for_status()

        result = _FlakyDetectionContext(**response.json())
        if len(result.existing_test_names) == 0:
            raise RuntimeError(
                f"No existing tests found for '{self.full_repository_name}' repository",
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
        if test in self._context.existing_test_names:
            return False

        if len(test) > self._context.max_test_name_length:
            self._over_length_tests.add(test)
            return False

        self._new_test_metrics[test].add_duration(duration)

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
            len(self._new_test_metrics) == 0
            and self.last_collected_test
            and self.last_collected_test not in self._context.existing_test_names
        ):
            allocation = {
                self.last_collected_test: self._context.max_test_execution_count,
            }
        else:
            allocation = _allocate_test_retries(
                self._get_budget_duration(),
                {
                    test: metrics.initial_duration
                    for test, metrics in self._new_test_metrics.items()
                },
                self._context.min_test_execution_count,
                self._context.max_test_execution_count,
            )

        items_to_retry = [item for item in session.items if item.nodeid in allocation]

        result = []
        for item in items_to_retry:
            expected_retries = int(allocation[item.nodeid])
            existing_retries = int(
                self._new_test_metrics[item.nodeid].scheduled_retry_count,
            )

            remaining_retries = max(0, expected_retries - existing_retries)
            for _ in range(remaining_retries):
                # The parent is a class or a module in our case. It should
                # always be defined, but we handle it gracefully just in case.
                if not item.parent:
                    continue

                self._new_test_metrics[item.nodeid].scheduled_retry_count += 1

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
            result += (
                f"{os.linesep}- Skipped {len(self._over_length_tests)} "
                f"test{'s' if len(self._over_length_tests) > 1 else ''}:"
            )
            for test in self._over_length_tests:
                result += (
                    f"{os.linesep}    • '{test}' has not been tested multiple times because the name of the test "
                    f"exceeds our limit of {self._context.max_test_name_length} characters"
                )

        if not self._new_test_metrics:
            result += f"{os.linesep}- No new tests detected, but we are watching 👀"

            return result

        total_retry_duration_seconds = sum(
            metrics.total_duration.total_seconds()
            for metrics in self._new_test_metrics.values()
        )
        budget_duration_seconds = self._get_budget_duration().total_seconds()
        result += (
            f"{os.linesep}- Used {total_retry_duration_seconds / budget_duration_seconds * 100:.2f} % of the budget "
            f"({total_retry_duration_seconds:.2f} s/{budget_duration_seconds:.2f} s)"
        )

        result += (
            f"{os.linesep}- Active for {len(self._new_test_metrics)} new "
            f"test{'s' if len(self._new_test_metrics) > 1 else ''}:"
        )
        for test, metrics in self._new_test_metrics.items():
            if metrics.scheduled_retry_count == 0:
                result += (
                    f"{os.linesep}    • '{test}' is too slow to be tested at least "
                    f"{self._context.min_test_execution_count} times within the budget"
                )
                continue

            if metrics.retry_count < metrics.scheduled_retry_count:
                result += (
                    f"{os.linesep}    • '{test}' has been tested only {metrics.retry_count} "
                    f"time{'s' if metrics.retry_count > 1 else ''} instead of {metrics.scheduled_retry_count} "
                    f"time{'s' if metrics.scheduled_retry_count > 1 else ''} to avoid exceeding the budget"
                )
                continue

            retry_duration_seconds = metrics.total_duration.total_seconds()
            result += (
                f"{os.linesep}    • '{test}' has been tested {metrics.retry_count} "
                f"time{'s' if metrics.retry_count > 1 else ''} using approx. "
                f"{retry_duration_seconds / budget_duration_seconds * 100:.2f} % of the budget "
                f"({retry_duration_seconds:.2f} s/{budget_duration_seconds:.2f} s)"
            )

        return result

    def _get_budget_duration(self) -> datetime.timedelta:
        """
        Calculate the budget duration based on a percentage of total test
        execution time.

        The budget ensures there's always a minimum time allocated of
        'self._context.min_budget_duration' even for very short test suites,
        preventing overly restrictive retry policies when the total test
        duration is small.
        """
        return max(
            self._context.budget_ratio * self._total_test_durations,
            self._context.min_budget_duration,
        )


def _select_affordable_tests(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
    min_test_execution_count: int,
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
        expected_retries_duration = duration * min_test_execution_count
        if expected_retries_duration <= budget_per_test:
            result[test] = duration

    return result


def _allocate_test_retries(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
    min_test_execution_count: int,
    max_test_execution_count: int,
) -> typing.Dict[str, int]:
    """
    Distribute retries within a fixed time budget.

    Why this shape:

    1. First, drop tests that aren't affordable (cannot reach
    `min_test_execution_count` within the budget). This avoids wasting time on
    tests that would starve the rest.

    2. Then allocate from fastest to slowest to free budget early: fast tests
    often hit `max_test_execution_count`; when capped, leftover time rolls over to
    slower tests.

    3. At each step we recompute a fair per-test slice from the remaining budget
    and remaining tests, so the distribution adapts as we go.
    """

    allocation: typing.Dict[str, int] = {}

    affordable_test_durations = _select_affordable_tests(
        budget_duration,
        test_durations,
        min_test_execution_count,
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
            allocation[test] = max_test_execution_count
            continue

        allocation[test] = min(budget_per_test // duration, max_test_execution_count)

    return allocation
