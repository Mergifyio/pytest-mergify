import dataclasses
import datetime
import os
import typing

import _pytest
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
    _global_deadline: typing.Optional[datetime.datetime] = dataclasses.field(
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

    log_messages: typing.List[str] = dataclasses.field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.log_messages.append("Initializing FlakyDetector")
        self._context = self._fetch_context()

    def _fetch_context(self) -> _FlakyDetectionContext:
        self.log_messages.append(
            f"Fetching flaky detection context for repository '{self.full_repository_name}' in mode '{self.mode}'"
        )
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

        self.log_messages.append(
            f"Fetched context: {len(result.existing_test_names)} existing tests, {len(result.unhealthy_test_names)} unhealthy tests"
        )
        return result

    def try_fill_metrics_from_report(self, report: _pytest.reports.TestReport) -> bool:
        self.log_messages.append(
            f"Considering test '{report.nodeid}' with outcome '{report.outcome}'"
        )

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

        self.log_messages.append(
            f"Filled metrics for test '{report.nodeid}': {metrics}"
        )

        return True

    def filter_context_tests_with_session(
        self, items: typing.List[_pytest.nodes.Item]
    ) -> None:
        session_tests = {item.nodeid for item in items}

        self.log_messages.append(
            f"Session has {len(session_tests)} tests: {session_tests}"
        )

        original_existing_count = len(self._context.existing_test_names)
        original_unhealthy_count = len(self._context.unhealthy_test_names)

        self.log_messages.append(
            f"Before filtering: {original_existing_count} existing tests, {original_unhealthy_count} unhealthy tests"
        )

        self._context.existing_test_names = [
            test for test in self._context.existing_test_names if test in session_tests
        ]
        self._context.unhealthy_test_names = [
            test for test in self._context.unhealthy_test_names if test in session_tests
        ]

        self.log_messages.append(
            f"After filtering: {len(self._context.existing_test_names)} existing tests, {len(self._context.unhealthy_test_names)} unhealthy tests"
        )

    def is_test_tracked(self, test: str) -> bool:
        result = test in self._test_metrics
        self.log_messages.append(
            f"Test '{test}' is {'tracked' if result else 'not tracked'}"
        )
        return result

    def get_rerun_count_for_test(self, test: str) -> int:
        metrics = self._test_metrics.get(test)
        if not metrics:
            self.log_messages.append(f"No metrics found for test '{test}'")
            return 0

        budget_per_test = (
            self._get_duration_before_test_deadline(test)
            / self._count_remaining_tests()
        )
        result = self._get_normalized_rerun_count(
            budget_per_test=budget_per_test,
            initial_duration=metrics.initial_duration,
        )

        metrics.is_processed = True
        metrics.scheduled_rerun_count = result

        self.log_messages.append(
            f"Scheduled {result} reruns for test '{test}' with budget per test {budget_per_test}"
        )

        return result

    def _get_normalized_rerun_count(
        self, budget_per_test: datetime.timedelta, initial_duration: datetime.timedelta
    ) -> int:
        self.log_messages.append(
            f"Calculating normalized rerun count with budget {budget_per_test} and initial duration {initial_duration}"
        )
        if initial_duration == datetime.timedelta():
            count = self._context.max_test_execution_count
        else:
            count = int(budget_per_test / initial_duration)

        result = min(count, self._context.max_test_execution_count)

        if result < self._context.min_test_execution_count:
            result = 0

        self.log_messages.append(f"Normalized rerun count: {result}")
        return result

    def should_abort_reruns(self, test: str) -> bool:
        """
        Determines if a test can be rerun within its deadline.

        We must ensure there's enough time remaining before the deadline to
        complete another full test execution. This prevents starting a rerun
        that would exceed the deadline and potentially timeout.
        """
        self.log_messages.append(f"Checking if reruns should abort for test '{test}'")
        metrics = self._test_metrics.get(test)
        if not metrics or not metrics.deadline:
            self.log_messages.append(
                f"No deadline or metrics for test '{test}', not aborting"
            )
            return False

        projected_completion = (
            datetime.datetime.now(datetime.timezone.utc) + metrics.initial_duration
        )
        result = projected_completion >= metrics.deadline
        self.log_messages.append(
            f"Projected completion {projected_completion}, deadline {metrics.deadline}, abort: {result}"
        )
        return result

    def make_report(self) -> str:
        self.log_messages.append("Generating flaky detection report")
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

        if self.log_messages:
            result += f"{os.linesep}Log Messages:"
            for message in self.log_messages:
                result += f"{os.linesep}- {message}"

        return result

    def set_global_deadline(self) -> None:
        self._global_deadline = (
            datetime.datetime.now(datetime.timezone.utc) + self._get_budget_duration()
        )

        self.log_messages.append(
            f"Global deadline set to {self._global_deadline.isoformat()}"
        )

    def set_test_deadline(
        self, test: str, timeout: typing.Optional[datetime.timedelta] = None
    ) -> None:
        metrics = self._test_metrics.get(test)
        if not metrics:
            self.log_messages.append(f"No metrics found for test '{test}'")
            return

        metrics.deadline = self._global_deadline
        self.log_messages.append(
            f"Set deadline for test '{test}' to global deadline: {self._global_deadline}"
        )

        if not timeout:
            return

        # Leave a margin of 10 %. Better safe than sorry. We don't want to crash
        # the CI.
        safe_timeout = timeout * 0.9
        timeout_deadline = datetime.datetime.now(datetime.timezone.utc) + safe_timeout
        self.log_messages.append(
            f"Calculated safe timeout deadline for test '{test}': {timeout_deadline}"
        )

        if not metrics.deadline or timeout_deadline < metrics.deadline:
            metrics.deadline = timeout_deadline
            metrics.prevented_timeout = True
            self.log_messages.append(
                f"Updated deadline for test '{test}' to timeout deadline and set prevented_timeout"
            )

    def is_last_rerun_for_test(self, test: str) -> bool:
        "Returns true if the given test exists and this is its last rerun."
        self.log_messages.append(
            f"Checking if this is the last rerun for test '{test}'"
        )
        metrics = self._test_metrics.get(test)
        if not metrics:
            self.log_messages.append(f"No metrics found for test '{test}'")
            return False

        result = (
            metrics.scheduled_rerun_count != 0
            and metrics.scheduled_rerun_count + 1  # Add the initial execution.
            == metrics.rerun_count
        )
        self.log_messages.append(f"Is last rerun for test '{test}': {result}")
        return result

    def suspend_item_finalizers(self, item: _pytest.nodes.Item) -> None:
        """
        Suspend all finalizers except the ones at the function-level.

        See: https://github.com/pytest-dev/pytest-rerunfailures/blob/master/src/pytest_rerunfailures.py#L532-L538
        """
        self.log_messages.append(f"Suspending item finalizers for item '{item.nodeid}'")
        if item not in item.session._setupstate.stack:
            self.log_messages.append(f"Item '{item.nodeid}' not in setupstate stack")
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
        self.log_messages.append(f"Restoring item finalizers for item '{item.nodeid}'")
        item.session._setupstate.stack.update(self._suspended_item_finalizers)
        self._suspended_item_finalizers.clear()

    def _count_remaining_tests(self) -> int:
        if self.mode == "new":
            tests = self._context.existing_test_names
        elif self.mode == "unhealthy":
            tests = self._context.unhealthy_test_names

        already_processed_tests = {
            test for test, metrics in self._test_metrics.items() if metrics.is_processed
        }

        remaining = max(len(tests) - len(already_processed_tests), 1)
        self.log_messages.append(
            f"Counting remaining tests in {self.mode} mode: {len(tests)} total, {len(already_processed_tests)} processed, {remaining} remaining"
        )

        return remaining

    def _get_budget_duration(self) -> datetime.timedelta:
        total_duration = self._context.existing_tests_mean_duration * len(
            self._context.existing_test_names
        )
        self.log_messages.append(
            f"Calculated total duration for existing tests: {total_duration}"
        )

        if self.mode == "new":
            ratio = self._context.budget_ratio_for_new_tests
        elif self.mode == "unhealthy":
            ratio = self._context.budget_ratio_for_unhealthy_tests
        self.log_messages.append(f"Using budget ratio {ratio} for mode '{self.mode}'")

        # NOTE(remyduthu): We want to ensure a minimum duration even for very short test suites.
        budget_duration = max(ratio * total_duration, self._context.min_budget_duration)
        self.log_messages.append(
            f"Budget duration: {budget_duration} (max of {ratio * total_duration} and {self._context.min_budget_duration})"
        )
        return budget_duration

    def _get_duration_before_test_deadline(self, test: str) -> datetime.timedelta:
        self.log_messages.append(f"Getting duration before deadline for test '{test}'")
        metrics = self._test_metrics[test]
        if not metrics or not metrics.deadline:
            self.log_messages.append(
                f"No deadline set for test '{test}', returning zero duration"
            )
            return datetime.timedelta()

        duration = max(
            metrics.deadline - datetime.datetime.now(datetime.timezone.utc),
            datetime.timedelta(),
        )
        self.log_messages.append(
            f"Duration before deadline for test '{test}': {duration}"
        )
        return duration
