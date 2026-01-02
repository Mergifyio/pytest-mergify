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

    initial_setup_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )
    initial_call_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )
    initial_teardown_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )

    @property
    def initial_duration(self) -> datetime.timedelta:
        """
        Represents the duration of the initial run of the test including the 3
        phases of the protocol (setup, call, teardown).
        """
        return (
            self.initial_setup_duration
            + self.initial_call_duration
            + self.initial_teardown_duration
        )

    # NOTE(remyduthu): We need this flag because we may have processed a test
    # without scheduling reruns for it (e.g., because it was too slow).
    is_processed: bool = dataclasses.field(default=False)

    rerun_count: int = dataclasses.field(default=0)
    "Represents the number of times the test has been rerun so far."

    scheduled_rerun_count: int = dataclasses.field(default=0)
    "Represents the number of reruns that have been scheduled for this test depending on the budget."

    deadline: typing.Optional[datetime.datetime] = dataclasses.field(default=None)

    prevented_timeout: bool = dataclasses.field(default=False)

    total_duration: datetime.timedelta = dataclasses.field(
        default_factory=datetime.timedelta
    )
    "Represents the total duration spent executing this test, including reruns."

    def fill_from_report(self, report: _pytest.reports.TestReport) -> None:
        duration = datetime.timedelta(seconds=report.duration)

        if report.when == "setup" and not self.initial_setup_duration:
            self.initial_setup_duration = duration
        elif report.when == "call" and not self.initial_call_duration:
            self.initial_call_duration = duration
        elif report.when == "teardown" and not self.initial_teardown_duration:
            self.initial_teardown_duration = duration

        if report.when == "call":
            self.rerun_count += 1

        self.total_duration += duration

    def expected_duration(self) -> datetime.timedelta:
        return self.initial_duration * self.scheduled_rerun_count


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

    def try_fill_metrics_from_report(self, report: _pytest.reports.TestReport) -> bool:
        if report.outcome not in ["failed", "passed", "rerun"]:
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
        metrics.fill_from_report(report)

        return True

    def filter_context_tests_with_session(self, session: _pytest.main.Session) -> None:
        session_tests = {item.nodeid for item in session.items}
        self._context.existing_test_names = [
            test for test in self._context.existing_test_names if test in session_tests
        ]
        self._context.unhealthy_test_names = [
            test for test in self._context.unhealthy_test_names if test in session_tests
        ]

    def is_test_tracked(self, test: str) -> bool:
        return test in self._test_metrics

    def get_rerun_count_for_test(self, test: str) -> int:
        metrics = self._test_metrics.get(test)
        if not metrics:
            return 0

        result = self._get_normalized_rerun_count(
            budget_per_test=self._get_duration_before_test_deadline(test)
            / self._count_remaining_tests(),
            initial_duration=metrics.initial_duration,
        )

        metrics.is_processed = True
        metrics.scheduled_rerun_count = result

        return result

    def _get_normalized_rerun_count(
        self, budget_per_test: datetime.timedelta, initial_duration: datetime.timedelta
    ) -> int:
        if initial_duration == datetime.timedelta():
            count = self._context.max_test_execution_count
        else:
            count = int(budget_per_test / initial_duration)

        result = min(count, self._context.max_test_execution_count)

        if result < self._context.min_test_execution_count:
            return 0

        return result

    def should_abort_reruns(self, test: str) -> bool:
        """
        Determines if a test can be rerun within its deadline.

        We must ensure there's enough time remaining before the deadline to
        complete another full test execution. This prevents starting a rerun
        that would exceed the deadline and potentially timeout.
        """
        metrics = self._test_metrics.get(test)
        if not metrics or not metrics.deadline:
            return False

        projected_completion = (
            datetime.datetime.now(datetime.timezone.utc) + metrics.initial_duration
        )
        return projected_completion >= metrics.deadline

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

        tests_prevented_from_timeout = [
            test
            for test, metrics in self._test_metrics.items()
            if metrics.prevented_timeout
        ]
        if tests_prevented_from_timeout:
            result += (
                f"{os.linesep}⚠️ Reduced reruns for the following "
                f"test{'s' if len(tests_prevented_from_timeout) else ''} to respect 'pytest-timeout':"
            )

            for test in [
                test
                for test, metrics in self._test_metrics.items()
                if metrics.prevented_timeout
            ]:
                result += f"{os.linesep}    • '{test}'"

            result += (
                f"{os.linesep}To improve flaky detection and prevent fixture-level timeouts from limiting reruns, enable function-only timeouts. "
                f"Reference: https://github.com/pytest-dev/pytest-timeout?tab=readme-ov-file#avoiding-timeouts-in-fixtures"
            )

        return result

    def set_deadline(self) -> None:
        self._deadline = (
            datetime.datetime.now(datetime.timezone.utc) + self._get_budget_duration()
        )

    def set_test_deadline(
        self, test: str, timeout: typing.Optional[datetime.timedelta] = None
    ) -> None:
        metrics = self._test_metrics.get(test)
        if not metrics:
            return

        metrics.deadline = self._deadline  # Use global deadline by default.

        if not timeout:
            return

        # Leave a margin of 10 %. Better safe than sorry. We don't want to crash
        # the CI.
        safe_timeout = timeout * 0.9
        timeout_deadline = datetime.datetime.now(datetime.timezone.utc) + safe_timeout
        if not metrics.deadline or timeout_deadline < metrics.deadline:
            metrics.deadline = timeout_deadline
            metrics.prevented_timeout = True

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

    def _get_duration_before_test_deadline(self, test: str) -> datetime.timedelta:
        metrics = self._test_metrics[test]
        if not metrics or not metrics.deadline:
            return datetime.timedelta()

        return max(
            metrics.deadline - datetime.datetime.now(datetime.timezone.utc),
            datetime.timedelta(),
        )
