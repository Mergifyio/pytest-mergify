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
    budget_ratio_for_new_tests: float
    budget_ratio_for_unhealthy_tests: float
    existing_test_names: typing.List[str]
    existing_tests_mean_duration_ms: int
    unhealthy_test_names: typing.List[str]
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
class _TestMetrics:
    "Represents metrics collected for a test."

    initial_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )
    "Represents the duration of the initial execution of the test."

    # NOTE(remyduthu): We need this flag because we may have processed a test
    # without scheduling reruns for it (e.g., because it was too slow).
    is_processed: bool = dataclasses.field(default=False)

    rerun_count: int = dataclasses.field(default=0)
    "Represents the number of times the test has been rerun so far."

    scheduled_rerun_count: int = dataclasses.field(default=0)
    "Represents the number of reruns that have been scheduled for this test depending on the budget."

    total_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )
    "Represents the total duration spent executing this test, including reruns."

    def add_duration(self, duration: datetime.timedelta) -> None:
        if not self.initial_duration:
            self.initial_duration = duration

        self.rerun_count += 1
        self.total_duration += duration


@dataclasses.dataclass
class FlakyDetector:
    token: str
    url: str
    full_repository_name: str
    mode: typing.Literal["new", "unhealthy"]

    _context: _FlakyDetectionContext = dataclasses.field(init=False)
    _deadline: typing.Optional[datetime.datetime] = dataclasses.field(
        init=False, default=None
    )
    _test_metrics: typing.Dict[str, _TestMetrics] = dataclasses.field(
        init=False, default_factory=dict
    )
    _over_length_tests: typing.Set[str] = dataclasses.field(
        init=False, default_factory=set
    )

    _suspended_item_finalizers: typing.Dict[_pytest.nodes.Node, typing.Any] = (
        dataclasses.field(
            init=False,
            default_factory=dict,
        )
    )
    """
    Storage for temporarily suspended fixture finalizers during flaky detection.

    Pytest maintains a `session._setupstate.stack` dictionary that tracks which
    fixture teardown functions (finalizers) need to run when a scope ends:

        {
            <test_item>: [(finalizer_fn, ...), exception_info],     # Function scope.
            <class_node>: [(finalizer_fn, ...), exception_info],    # Class scope.
            <module_node>: [(finalizer_fn, ...), exception_info],   # Module scope.
            <session>: [(finalizer_fn, ...), exception_info]        # Session scope.
        }

    When rerunning a test, we want to:

    - Tear down and re-setup function-scoped fixtures for each rerun.
    - Keep higher-scoped fixtures alive across all reruns.

    This approach is inspired by pytest-rerunfailures:
    https://github.com/pytest-dev/pytest-rerunfailures/blob/master/src/pytest_rerunfailures.py#L503-L542
    """

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
        if self.mode == "new" and len(result.existing_test_names) == 0:
            raise RuntimeError(
                f"No existing tests found for '{self.full_repository_name}' repository",
            )

        return result

    def detect_from_report(self, report: _pytest.reports.TestReport) -> bool:
        if report.when != "call":
            return False

        if report.outcome not in ["failed", "passed"]:
            return False

        test = report.nodeid

        if self.mode == "new" and test in self._context.existing_test_names:
            return False
        elif (
            self.mode == "unhealthy" and test not in self._context.unhealthy_test_names
        ):
            return False

        if len(test) > self._context.max_test_name_length:
            self._over_length_tests.add(test)
            return False

        metrics = self._test_metrics.setdefault(test, _TestMetrics())
        metrics.add_duration(datetime.timedelta(seconds=report.duration))

        return True

    def filter_context_tests_with_session(self, session: _pytest.main.Session) -> None:
        session_tests = {item.nodeid for item in session.items}
        self._context.existing_test_names = [
            test for test in self._context.existing_test_names if test in session_tests
        ]
        self._context.unhealthy_test_names = [
            test for test in self._context.unhealthy_test_names if test in session_tests
        ]

    def get_rerun_count_for_test(self, test: str) -> int:
        metrics = self._test_metrics.get(test)
        if not metrics:
            return 0

        budget_per_test = (
            self._get_duration_before_deadline() / self._count_remaining_tests()
        )
        result = int(budget_per_test / metrics.initial_duration)
        result = min(result, self._context.max_test_execution_count)

        # NOTE(remyduthu): Count as processed even if it's too slow.
        metrics.is_processed = True

        if result < self._context.min_test_execution_count:
            return 0

        metrics.scheduled_rerun_count = result

        return result

    def is_deadline_exceeded(self) -> bool:
        return (
            self._deadline is not None
            and datetime.datetime.now(datetime.timezone.utc) >= self._deadline
        )

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

        if not self._test_metrics:
            result += (
                f"{os.linesep}- No {self.mode} tests detected, but we are watching 👀"
            )

            return result

        total_rerun_duration_seconds = sum(
            metrics.total_duration.total_seconds()
            for metrics in self._test_metrics.values()
        )
        budget_duration_seconds = self._get_budget_duration().total_seconds()
        result += (
            f"{os.linesep}- Used {total_rerun_duration_seconds / budget_duration_seconds * 100:.2f} % of the budget "
            f"({total_rerun_duration_seconds:.2f} s/{budget_duration_seconds:.2f} s)"
        )

        result += (
            f"{os.linesep}- Active for {len(self._test_metrics)} {self.mode} "
            f"test{'s' if len(self._test_metrics) > 1 else ''}:"
        )
        for test, metrics in self._test_metrics.items():
            if metrics.scheduled_rerun_count == 0:
                result += (
                    f"{os.linesep}    • '{test}' is too slow to be tested at least "
                    f"{self._context.min_test_execution_count} times within the budget"
                )
                continue

            if metrics.rerun_count < metrics.scheduled_rerun_count:
                result += (
                    f"{os.linesep}    • '{test}' has been tested only {metrics.rerun_count} "
                    f"time{'s' if metrics.rerun_count > 1 else ''} instead of {metrics.scheduled_rerun_count} "
                    f"time{'s' if metrics.scheduled_rerun_count > 1 else ''} to avoid exceeding the budget"
                )
                continue

            rerun_duration_seconds = metrics.total_duration.total_seconds()
            result += (
                f"{os.linesep}    • '{test}' has been tested {metrics.rerun_count} "
                f"time{'s' if metrics.rerun_count > 1 else ''} using approx. "
                f"{rerun_duration_seconds / budget_duration_seconds * 100:.2f} % of the budget "
                f"({rerun_duration_seconds:.2f} s/{budget_duration_seconds:.2f} s)"
            )

        return result

    def set_deadline(self) -> None:
        self._deadline = (
            datetime.datetime.now(datetime.timezone.utc)
            + self._context.existing_tests_mean_duration
            + self._get_budget_duration()
        )

    def is_last_rerun_for_test(self, test: str) -> bool:
        "Returns true if the given test exists and this is its last rerun."

        metrics = self._test_metrics.get(test)
        if not metrics:
            return False

        return (
            metrics.scheduled_rerun_count != 0
            and metrics.scheduled_rerun_count + 1  # Add the initial execution.
            == metrics.rerun_count
        )

    def suspend_item_finalizers(self, item: _pytest.nodes.Item) -> None:
        """
        Suspend all finalizers except the ones at the function-level.

        See: https://github.com/pytest-dev/pytest-rerunfailures/blob/master/src/pytest_rerunfailures.py#L532-L538
        """

        if item not in item.session._setupstate.stack:
            return

        for stacked_item in list(item.session._setupstate.stack.keys()):
            if stacked_item == item:
                continue

            if stacked_item not in self._suspended_item_finalizers:
                self._suspended_item_finalizers[stacked_item] = (
                    item.session._setupstate.stack[stacked_item]
                )
            del item.session._setupstate.stack[stacked_item]

    def restore_item_finalizers(self, item: _pytest.nodes.Item) -> None:
        """
        Restore previously suspended finalizers.

        See: https://github.com/pytest-dev/pytest-rerunfailures/blob/master/src/pytest_rerunfailures.py#L540-L542
        """

        item.session._setupstate.stack.update(self._suspended_item_finalizers)
        self._suspended_item_finalizers.clear()

    def _count_remaining_tests(self) -> int:
        return sum(
            1 for metrics in self._test_metrics.values() if not metrics.is_processed
        )

    def _get_budget_duration(self) -> datetime.timedelta:
        total_duration = self._context.existing_tests_mean_duration * len(
            self._context.existing_test_names
        )

        if self.mode == "new":
            ratio = self._context.budget_ratio_for_new_tests
        elif self.mode == "unhealthy":
            ratio = self._context.budget_ratio_for_unhealthy_tests

        # NOTE(remyduthu): We want to ensure a minimum duration even for very short test suites.
        return max(ratio * total_duration, self._context.min_budget_duration)

    def _get_duration_before_deadline(self) -> datetime.timedelta:
        if not self._deadline:
            return datetime.timedelta()

        return max(
            self._deadline - datetime.datetime.now(datetime.timezone.utc),
            datetime.timedelta(),
        )
