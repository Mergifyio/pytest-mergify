import dataclasses
import datetime
import json
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

    rerun_count: int = dataclasses.field(default=0)
    "Represents the number of times the test has been rerun so far."

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

    def remaining_time(self) -> datetime.timedelta:
        if not self.deadline:
            return datetime.timedelta()

        return max(
            self.deadline - datetime.datetime.now(datetime.timezone.utc),
            datetime.timedelta(),
        )

    def will_exceed_deadline(self) -> bool:
        if not self.deadline:
            return True

        return (
            datetime.datetime.now(datetime.timezone.utc) + self.initial_duration
            >= self.deadline
        )


@dataclasses.dataclass
class FlakyDetector:
    token: str
    url: str
    full_repository_name: str
    mode: typing.Literal["new", "unhealthy"]

    _context: _FlakyDetectionContext = dataclasses.field(init=False)
    _test_metrics: typing.Dict[str, _TestMetrics] = dataclasses.field(
        init=False, default_factory=dict
    )
    _over_length_tests: typing.Set[str] = dataclasses.field(
        init=False, default_factory=set
    )

    _available_budget_duration: datetime.timedelta = dataclasses.field(
        init=False, default_factory=datetime.timedelta
    )
    _tests_to_process: typing.List[str] = dataclasses.field(
        init=False, default_factory=list
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

    _debug_logs: typing.List[utils.StructuredLog] = dataclasses.field(
        init=False, default_factory=list
    )

    _is_xdist: bool = dataclasses.field(init=False, default=False)

    def __post_init__(self) -> None:
        self._context = self._fetch_context()

    @classmethod
    def from_context(
        cls,
        context_dict: typing.Dict[str, typing.Any],
        mode: typing.Literal["new", "unhealthy"],
    ) -> "FlakyDetector":
        """Construct from serialized context dict, skipping the API call."""
        instance = cls.__new__(cls)
        instance.token = ""
        instance.url = ""
        instance.full_repository_name = ""
        instance.mode = mode
        instance._context = _FlakyDetectionContext(**context_dict)
        instance._test_metrics = {}
        instance._over_length_tests = set()
        instance._available_budget_duration = datetime.timedelta()
        instance._tests_to_process = []
        instance._suspended_item_finalizers = {}
        instance._debug_logs = []
        instance._is_xdist = True
        return instance

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

    def try_fill_metrics_from_report(self, report: _pytest.reports.TestReport) -> None:
        test = report.nodeid

        if report.outcome == "skipped":
            # Remove metrics for skipped tests. Setup phase may have passed and
            # initialized metrics before call phase was skipped.
            self._test_metrics.pop(test, None)
            return

        if test not in self._tests_to_process:
            return

        if len(test) > self._context.max_test_name_length:
            self._over_length_tests.add(test)
            return

        if test not in self._test_metrics:
            if report.when != "setup":
                # Metrics have been removed (e.g. for a skipped test), do nothing.
                return

            # Initialize metrics after setup phase.
            self._test_metrics[test] = _TestMetrics()

        self._test_metrics[test].fill_from_report(report)

    def prepare_for_session(self, session: _pytest.main.Session) -> None:
        tests_in_session = {item.nodeid for item in session.items}
        existing_tests_in_session = [
            test
            for test in self._context.existing_test_names
            if test in tests_in_session
        ]

        if self.mode == "new":
            self._tests_to_process = [
                test
                for test in tests_in_session
                if test not in existing_tests_in_session
            ]
        elif self.mode == "unhealthy":
            self._tests_to_process = [
                test
                for test in tests_in_session
                if test in self._context.unhealthy_test_names
            ]

        if self.mode == "new":
            budget_ratio = self._context.budget_ratio_for_new_tests
        elif self.mode == "unhealthy":
            budget_ratio = self._context.budget_ratio_for_unhealthy_tests

        total_duration = self._context.existing_tests_mean_duration * len(
            existing_tests_in_session
        )

        # We want to ensure a minimum duration even for very short test suites.
        self._available_budget_duration = max(
            budget_ratio * total_duration,
            self._context.min_budget_duration,
        )

    def is_test_too_slow(self, test: str) -> bool:
        metrics = self._test_metrics[test]

        return (
            metrics.initial_duration * self._context.min_test_execution_count
            > metrics.remaining_time()
        )

    def is_test_rerun(self, test: str) -> bool:
        """Returns `True` if the test has already completed its initial run and is
        now in a rerun, `False` otherwise."""
        return (
            metrics := self._test_metrics.get(test)
        ) is not None and metrics.rerun_count > 1

    def is_rerunning_test(self, test: str) -> bool:
        return (
            metrics := self._test_metrics.get(test)
        ) is not None and metrics.rerun_count >= 1

    def is_last_rerun_for_test(self, test: str) -> bool:
        metrics = self._test_metrics[test]

        will_exceed_deadline = metrics.will_exceed_deadline()
        will_exceed_rerun_count = (
            metrics.rerun_count >= self._context.max_test_execution_count
        )

        self._debug_logs.append(
            utils.StructuredLog.make(
                message="Check for last rerun",
                test=test,
                deadline=metrics.deadline.isoformat() if metrics.deadline else None,
                rerun_count=metrics.rerun_count,
                will_exceed_deadline=will_exceed_deadline,
                will_exceed_rerun_count=will_exceed_rerun_count,
            )
        )

        return will_exceed_deadline or will_exceed_rerun_count

    def make_report(self) -> str:
        """Generate terminal report by delegating to the shared report function."""
        serialized = self.to_serializable_metrics()
        return make_report_from_aggregated(
            context_dict=dataclasses.asdict(self._context),
            mode=self.mode,
            available_budget_duration_ms=self._available_budget_duration.total_seconds()
            * 1000,
            aggregated_metrics=serialized,
        )

    def set_test_deadline(
        self, test: str, timeout: typing.Optional[datetime.timedelta] = None
    ) -> None:
        metrics = self._test_metrics[test]

        if self._is_xdist:
            # Static allocation: equal share of total budget per test.
            per_test_budget = self._available_budget_duration / max(
                len(self._tests_to_process), 1
            )
            metrics.deadline = (
                datetime.datetime.now(datetime.timezone.utc) + per_test_budget
            )
            self._debug_logs.append(
                utils.StructuredLog.make(
                    message="Deadline set",
                    test=test,
                    available_budget=str(self._available_budget_duration),
                    is_xdist=True,
                    all_tests=len(self._tests_to_process),
                )
            )
        else:
            remaining_budget = self._get_remaining_budget_duration()
            remaining_tests = self._count_remaining_tests()

            # Distribute remaining budget equally across remaining tests.
            metrics.deadline = datetime.datetime.now(datetime.timezone.utc) + (
                remaining_budget / remaining_tests
            )
            self._debug_logs.append(
                utils.StructuredLog.make(
                    message="Deadline set",
                    test=test,
                    available_budget=str(self._available_budget_duration),
                    remaining_budget=str(remaining_budget),
                    all_tests=len(self._tests_to_process),
                    remaining_tests=remaining_tests,
                )
            )

        if not timeout:
            return

        # Leave a margin of 10 %. Better safe than sorry. We don't want to crash
        # the CI.
        safe_timeout = timeout * 0.9
        timeout_deadline = datetime.datetime.now(datetime.timezone.utc) + safe_timeout
        if not metrics.deadline or timeout_deadline < metrics.deadline:
            metrics.deadline = timeout_deadline
            metrics.prevented_timeout = True
            self._debug_logs.append(
                utils.StructuredLog.make(
                    message="Deadline updated to prevent timeout",
                    test=test,
                    timeout=str(timeout),
                    safe_timeout=str(safe_timeout),
                    deadline=metrics.deadline,
                )
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

    def to_serializable_metrics(self) -> typing.Dict[str, typing.Any]:
        """Serialize metrics for transport via xdist workeroutput."""
        return {
            "available_budget_duration_ms": self._available_budget_duration.total_seconds()
            * 1000,
            "test_metrics": {
                test: {
                    "rerun_count": metrics.rerun_count,
                    "total_duration_ms": metrics.total_duration.total_seconds() * 1000,
                    "initial_setup_duration_ms": metrics.initial_setup_duration.total_seconds()
                    * 1000,
                    "initial_call_duration_ms": metrics.initial_call_duration.total_seconds()
                    * 1000,
                    "initial_teardown_duration_ms": metrics.initial_teardown_duration.total_seconds()
                    * 1000,
                    "prevented_timeout": metrics.prevented_timeout,
                }
                for test, metrics in self._test_metrics.items()
            },
            "over_length_tests": list(self._over_length_tests),
            "debug_logs": [
                {
                    "timestamp": log.timestamp.isoformat(),
                    "message": log.message,
                    **log.attributes,
                }
                for log in self._debug_logs
            ],
        }

    def _count_remaining_tests(self) -> int:
        already_processed_tests = {
            test for test, metrics in self._test_metrics.items() if metrics.deadline
        }

        return max(len(self._tests_to_process) - len(already_processed_tests), 1)

    def _get_used_budget_duration(self) -> datetime.timedelta:
        return sum(
            (metrics.total_duration for metrics in self._test_metrics.values()),
            datetime.timedelta(),
        )

    def _get_remaining_budget_duration(self) -> datetime.timedelta:
        return max(
            self._available_budget_duration - self._get_used_budget_duration(),
            datetime.timedelta(),
        )


@dataclasses.dataclass
class XdistFlakyDetectionController:
    """Manages flaky detection state on the xdist controller side."""

    _context_dict: typing.Optional[typing.Dict[str, typing.Any]] = dataclasses.field(
        default=None
    )
    _mode: typing.Optional[str] = dataclasses.field(default=None)
    _aggregated_metrics: typing.Dict[str, typing.Any] = dataclasses.field(
        default_factory=lambda: {
            "test_metrics": {},
            "over_length_tests": [],
            "debug_logs": [],
        }
    )
    _available_budget_duration_ms: float = dataclasses.field(default=0.0)

    def extract_context_from_detector(self, detector: FlakyDetector) -> None:
        """Extract context from an already-loaded detector for distribution."""
        self._context_dict = dataclasses.asdict(detector._context)
        self._mode = detector.mode

    @property
    def has_context(self) -> bool:
        return self._context_dict is not None

    def populate_workerinput(self, workerinput: typing.Dict[str, typing.Any]) -> None:
        """Add flaky detection context to a worker's input dict."""
        if self._context_dict is not None:
            workerinput["flaky_detection_context"] = self._context_dict
            workerinput["flaky_detection_mode"] = self._mode

    def collect_worker_metrics(
        self, worker_metrics: typing.Dict[str, typing.Any]
    ) -> None:
        """Merge metrics received from a completed worker."""
        self._aggregated_metrics["test_metrics"].update(worker_metrics["test_metrics"])
        self._aggregated_metrics["over_length_tests"].extend(
            worker_metrics["over_length_tests"]
        )
        self._aggregated_metrics["debug_logs"].extend(worker_metrics["debug_logs"])

        # Budget is the same across all workers (deterministic). Use first received.
        if (
            self._available_budget_duration_ms == 0.0
            and "available_budget_duration_ms" in worker_metrics
        ):
            self._available_budget_duration_ms = worker_metrics[
                "available_budget_duration_ms"
            ]

    def make_report(self) -> str:
        """Generate terminal report from aggregated worker data."""
        assert self._context_dict is not None
        mode: typing.Literal["new", "unhealthy"] = (
            self._mode  # type: ignore[assignment]
            if self._mode in ("new", "unhealthy")
            else "new"
        )
        return make_report_from_aggregated(
            context_dict=self._context_dict,
            mode=mode,
            available_budget_duration_ms=self._available_budget_duration_ms,
            aggregated_metrics=self._aggregated_metrics,
        )


def make_report_from_aggregated(
    context_dict: typing.Dict[str, typing.Any],
    mode: typing.Literal["new", "unhealthy"],
    available_budget_duration_ms: float,
    aggregated_metrics: typing.Dict[str, typing.Any],
) -> str:
    """Generate report on the controller from aggregated worker metrics."""
    context = _FlakyDetectionContext(**context_dict)
    test_metrics = aggregated_metrics["test_metrics"]
    over_length_tests = aggregated_metrics["over_length_tests"]
    debug_logs = aggregated_metrics["debug_logs"]

    result = "🐛 Flaky detection"

    if over_length_tests:
        result += (
            f"{os.linesep}- Skipped {len(over_length_tests)} "
            f"test{'s' if len(over_length_tests) > 1 else ''}:"
        )
        for test in sorted(over_length_tests):
            result += (
                f"{os.linesep}    • '{test}' has not been tested multiple times because the name of the test "
                f"exceeds our limit of {context.max_test_name_length} characters"
            )

    if not test_metrics:
        result += f"{os.linesep}- No {mode} tests detected, but we are watching 👀"
        return result

    available_budget_seconds = available_budget_duration_ms / 1000
    used_budget_ms = sum(m["total_duration_ms"] for m in test_metrics.values())
    used_budget_seconds = used_budget_ms / 1000
    if available_budget_seconds > 0:
        result += (
            f"{os.linesep}- Used {used_budget_seconds / available_budget_seconds * 100:.2f} % of the budget "
            f"({used_budget_seconds:.2f} s/{available_budget_seconds:.2f} s)"
        )
    else:
        result += f"{os.linesep}- Used {used_budget_seconds:.2f} s (budget unavailable)"

    result += (
        f"{os.linesep}- Active for {len(test_metrics)} {mode} "
        f"test{'s' if len(test_metrics) > 1 else ''}:"
    )
    for test, m in sorted(test_metrics.items()):
        if m["rerun_count"] < context.min_test_execution_count:
            result += (
                f"{os.linesep}    • '{test}' is too slow to be tested at least "
                f"{context.min_test_execution_count} times within the budget"
            )
            continue

        rerun_duration_seconds = m["total_duration_ms"] / 1000
        if available_budget_seconds > 0:
            result += (
                f"{os.linesep}    • '{test}' has been tested {m['rerun_count']} "
                f"time{'s' if m['rerun_count'] > 1 else ''} using approx. "
                f"{rerun_duration_seconds / available_budget_seconds * 100:.2f} % of the budget "
                f"({rerun_duration_seconds:.2f} s/{available_budget_seconds:.2f} s)"
            )
        else:
            result += (
                f"{os.linesep}    • '{test}' has been tested {m['rerun_count']} "
                f"time{'s' if m['rerun_count'] > 1 else ''} "
                f"({rerun_duration_seconds:.2f} s)"
            )

    tests_prevented_from_timeout = [
        test for test, m in test_metrics.items() if m["prevented_timeout"]
    ]
    if tests_prevented_from_timeout:
        result += (
            f"{os.linesep}⚠️ Reduced reruns for the following "
            f"test{'s' if len(tests_prevented_from_timeout) > 1 else ''} to respect 'pytest-timeout':"
        )
        for test in tests_prevented_from_timeout:
            result += f"{os.linesep}    • '{test}'"

        result += (
            f"{os.linesep}To improve flaky detection and prevent fixture-level timeouts from limiting reruns, enable function-only timeouts. "
            f"Reference: https://github.com/pytest-dev/pytest-timeout?tab=readme-ov-file#avoiding-timeouts-in-fixtures"
        )

    if os.environ.get("PYTEST_MERGIFY_DEBUG") and debug_logs:
        result += f"{os.linesep}🔎 Debug Logs"
        for log in debug_logs:
            result += f"{os.linesep}{json.dumps(log)}"

    return result
