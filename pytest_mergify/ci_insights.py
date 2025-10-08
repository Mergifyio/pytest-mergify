import dataclasses
import datetime
import os
import random
import typing

import _pytest.main
import _pytest.nodes
import _pytest.terminal
import opentelemetry.sdk.resources
import requests
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider, export
from opentelemetry.semconv._incubating.attributes import vcs_attributes

import pytest_mergify.quarantine
import pytest_mergify.resources.ci as resources_ci
import pytest_mergify.resources.git as resources_git
import pytest_mergify.resources.github_actions as resources_gha
import pytest_mergify.resources.jenkins as resources_jenkins
import pytest_mergify.resources.mergify as resources_mergify
import pytest_mergify.resources.pytest as resources_pytest
from pytest_mergify import utils


class SynchronousBatchSpanProcessor(export.SimpleSpanProcessor):
    def __init__(self, exporter: export.SpanExporter) -> None:
        super().__init__(exporter)
        self.queue: typing.List[ReadableSpan] = []

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.span_exporter.export(self.queue)
        self.queue.clear()
        return True

    def on_end(self, span: ReadableSpan) -> None:
        if not span.context.trace_flags.sampled:
            return

        self.queue.append(span)


class SessionHardRaiser(requests.Session):  # type: ignore[misc]
    """Custom requests.Session that raises an exception on HTTP error."""

    def request(self, *args: typing.Any, **kwargs: typing.Any) -> requests.Response:
        response = super().request(*args, **kwargs)
        response.raise_for_status()
        return response


# NOTE(remyduthu): We are using a hard-coded budget for now, but the idea is to
# make it configurable in the future.
_DEFAULT_TEST_RETRY_BUDGET_RATIO = 0.1
_MAX_TEST_NAME_LENGTH = 65536
_MIN_TEST_RETRY_COUNT = 5
_MAX_TEST_RETRY_COUNT = 1000
_MIN_TEST_RETRY_BUDGET_DURATION = datetime.timedelta(seconds=1)


@dataclasses.dataclass
class MergifyCIInsights:
    token: typing.Optional[str] = dataclasses.field(
        default_factory=lambda: os.environ.get("MERGIFY_TOKEN")
    )
    repo_name: typing.Optional[str] = dataclasses.field(
        default_factory=utils.get_repository_name
    )
    api_url: str = dataclasses.field(
        default_factory=lambda: os.environ.get(
            "MERGIFY_API_URL", "https://api.mergify.com"
        )
    )
    branch_name: typing.Optional[str] = dataclasses.field(
        init=False,
        default=None,
    )
    exporter: typing.Optional[export.SpanExporter] = dataclasses.field(
        init=False, default=None
    )
    tracer: typing.Optional[opentelemetry.trace.Tracer] = dataclasses.field(
        init=False, default=None
    )
    tracer_provider: typing.Optional[opentelemetry.sdk.trace.TracerProvider] = (
        dataclasses.field(init=False, default=None)
    )
    test_run_id: str = dataclasses.field(
        init=False,
        default_factory=lambda: random.getrandbits(64).to_bytes(8, "big").hex(),
    )
    _existing_test_names: typing.List[str] = dataclasses.field(
        init=False,
        default_factory=list,
    )
    _flaky_detection_error_message: typing.Optional[str] = dataclasses.field(
        init=False,
        default=None,
    )
    _total_test_durations: datetime.timedelta = dataclasses.field(
        init=False, default=datetime.timedelta()
    )
    _new_test_durations_by_name: typing.Dict[str, datetime.timedelta] = (
        dataclasses.field(init=False, default_factory=dict)
    )
    _new_test_retry_count_by_name: typing.DefaultDict[str, int] = dataclasses.field(
        init=False, default_factory=lambda: typing.DefaultDict(int)
    )
    _over_length_test_names: typing.Set[str] = dataclasses.field(
        init=False,
        default_factory=set,
    )
    quarantined_tests: typing.Optional[pytest_mergify.quarantine.Quarantine] = (
        dataclasses.field(
            init=False,
            default=None,
        )
    )

    def __post_init__(self) -> None:
        if not utils.is_in_ci():
            return

        span_processor: SpanProcessor

        if os.environ.get("PYTEST_MERGIFY_DEBUG"):
            self.exporter = export.ConsoleSpanExporter()
            span_processor = SynchronousBatchSpanProcessor(self.exporter)
        elif utils.strtobool(os.environ.get("_PYTEST_MERGIFY_TEST", "false")):
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
                InMemorySpanExporter,
            )

            self.exporter = InMemorySpanExporter()
            span_processor = export.SimpleSpanProcessor(self.exporter)
        elif self.token and self.repo_name:
            try:
                owner, repo = utils.split_full_repo_name(self.repo_name)
            except utils.InvalidRepositoryFullNameError:
                return
            self.exporter = OTLPSpanExporter(
                session=SessionHardRaiser(),
                endpoint=f"{self.api_url}/v1/ci/{owner}/repositories/{repo}/traces",
                headers={"Authorization": f"Bearer {self.token}"},
                compression=Compression.Gzip,
            )
            span_processor = SynchronousBatchSpanProcessor(self.exporter)
        else:
            return

        resource = opentelemetry.sdk.resources.get_aggregated_resources(
            [
                resources_git.GitResourceDetector(),
                resources_ci.CIResourceDetector(),
                resources_gha.GitHubActionsResourceDetector(),
                resources_jenkins.JenkinsResourceDetector(),
                resources_pytest.PytestResourceDetector(),
                resources_mergify.MergifyResourceDetector(),
            ]
        )

        resource = resource.merge(
            opentelemetry.sdk.resources.Resource(
                {
                    "test.run.id": self.test_run_id,
                }
            )
        )

        self.tracer_provider = TracerProvider(resource=resource)

        self.tracer_provider.add_span_processor(span_processor)
        self.tracer = self.tracer_provider.get_tracer("pytest-mergify")

        # Retrieve the branch name based on the detected resources's attributes
        branch_name = resource.attributes.get(
            vcs_attributes.VCS_REF_BASE_NAME,
            resource.attributes.get(vcs_attributes.VCS_REF_HEAD_NAME),
        )
        if branch_name is not None:
            # `str` cast just for `mypy`
            self.branch_name = str(branch_name)

        self._load_flaky_detection()

        if self.token and self.repo_name and self.branch_name:
            self.quarantined_tests = pytest_mergify.quarantine.Quarantine(
                self.api_url,
                self.token,
                self.repo_name,
                self.branch_name,
            )

    def _add_new_test_duration(
        self,
        test_name: str,
        test_duration: datetime.timedelta,
    ) -> None:
        if test_name in self._new_test_durations_by_name:
            return

        if len(test_name) > _MAX_TEST_NAME_LENGTH:
            self._over_length_test_names.add(test_name)
            return

        self._new_test_durations_by_name[test_name] = test_duration

    def _is_flaky_detection_enabled(self) -> bool:
        return (
            self.token is not None
            and self.repo_name is not None
            # NOTE(remyduthu): Hide behind a feature flag for now.
            and utils.is_env_truthy("_MERGIFY_TEST_NEW_FLAKY_DETECTION")
        )

    def _is_flaky_detection_active(self) -> bool:
        return (
            self._is_flaky_detection_enabled()
            and self._flaky_detection_error_message is None
        )

    def _load_flaky_detection(self) -> None:
        if not self._is_flaky_detection_enabled():
            return

        try:
            self._existing_test_names = self._fetch_existing_test_names()
        except Exception as exception:
            self._flaky_detection_error_message = (
                f"Could not fetch existing test names: {str(exception)}"
            )

    def _fetch_existing_test_names(self) -> typing.List[str]:
        if not self.token or not self.repo_name or not self.branch_name:
            raise ValueError("'token', 'repo_name' and 'branch_name' are required")

        owner, repository = utils.split_full_repo_name(self.repo_name)

        response = requests.get(
            url=f"{self.api_url}/v1/ci/{owner}/tests/names",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"repository": repository, "branch": self.branch_name},
            timeout=10,
        )

        response.raise_for_status()

        return typing.cast(typing.List[str], response.json()["test_names"])

    def report_flaky_detection(
        self,
        terminalreporter: _pytest.terminal.TerminalReporter,
    ) -> None:
        if not self._is_flaky_detection_enabled():
            return

        if self._flaky_detection_error_message:
            terminalreporter.write_line(
                f"""⚠️  Flaky detection couldn't be enabled because of an error.

Common issues:
  • Your 'MERGIFY_TOKEN' might not be set or could be invalid
  • There might be a network connectivity issue with the Mergify API

📚 Documentation: https://docs.mergify.com/ci-insights/test-frameworks/pytest/
🔍 Details: {self._flaky_detection_error_message}""",
                yellow=True,
            )

            return

        terminalreporter.write_line(self._get_flaky_detection_report_message())

    def _get_flaky_detection_report_message(self) -> str:
        result = "🐛 Flaky detection"
        if self._over_length_test_names:
            result += (
                f"{os.linesep}- Skipped {len(self._over_length_test_names)} test(s):"
            )
            for name in self._over_length_test_names:
                result += (
                    f"{os.linesep}    • '{name}' has not been tested multiple "
                    f"times because the name of the test exceeds our limit of "
                    f"{_MAX_TEST_NAME_LENGTH} characters"
                )

        if not self._new_test_durations_by_name:
            result += f"{os.linesep}- No new tests detected, but we are watching 👀"

            return result

        total_retry_duration_seconds = sum(
            self._new_test_durations_by_name[test_name].total_seconds() * retry_count
            for test_name, retry_count in self._new_test_retry_count_by_name.items()
            if retry_count > 0
        )
        budget_duration_seconds = self._get_budget_duration().total_seconds()
        result += (
            f"{os.linesep}- Used {total_retry_duration_seconds / budget_duration_seconds * 100:.2f} % "
            f"of the budget ({total_retry_duration_seconds:.2f} s/{budget_duration_seconds:.2f} s)"
        )

        result += f"{os.linesep}- Active for {len(self._new_test_durations_by_name)} new test(s):"
        for test, duration in self._new_test_durations_by_name.items():
            retry_count = self._new_test_retry_count_by_name.get(test, 0)
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

    def handle_flaky_detection_for_report(
        self,
        report: _pytest.reports.TestReport,
    ) -> None:
        if not self._is_flaky_detection_active():
            return

        if report.outcome not in ["failed", "passed"]:
            return

        test_duration = datetime.timedelta(seconds=report.duration)
        self._total_test_durations += test_duration

        test_name = report.nodeid
        if test_name in self._existing_test_names:
            return

        self._add_new_test_duration(test_name, test_duration)

        if self.tracer:
            opentelemetry.trace.get_current_span().set_attributes(
                {"cicd.test.new": True}
            )

    def run_flaky_detection(self, session: _pytest.main.Session) -> None:
        if not self._is_flaky_detection_active():
            return

        allocation = _allocate_test_retries(
            self._get_budget_duration(),
            self._new_test_durations_by_name,
        )
        items_to_retry = [item for item in session.items if item.nodeid in allocation]

        # This deadline acts as an additional security measure to make sure
        # we'll not exceed the actual budget even if a test takes more time to
        # run than expected.
        budget_deadline = (
            datetime.datetime.now(datetime.timezone.utc) + self._get_budget_duration()
        )
        for item in items_to_retry:
            for _ in range(allocation[item.nodeid]):
                if datetime.datetime.now(datetime.timezone.utc) > budget_deadline:
                    return

                self._new_test_retry_count_by_name[item.nodeid] += 1
                item.ihook.pytest_runtest_protocol(item=item, nextitem=None)

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

    def mark_test_as_quarantined_if_needed(self, item: _pytest.nodes.Item) -> bool:
        """
        Returns `True` if the test was marked as quarantined, otherwise returns `False`.
        """
        if self.quarantined_tests is not None and item in self.quarantined_tests:
            self.quarantined_tests.mark_test_as_quarantined(item)
            return True

        return False


def _select_affordable_tests(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
) -> typing.Dict[str, datetime.timedelta]:
    """
    Select tests that can be retried within the given budget.

    This ensures we don't select tests that would exceed our time constraints
    even with the minimum number of retries.
    """

    result = {}
    for test, duration in test_durations.items():
        if duration * _MIN_TEST_RETRY_COUNT <= budget_duration / len(test_durations):
            result[test] = duration

    return result


def _allocate_test_retries(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
) -> typing.Dict[str, int]:
    """
    Distributes test retries based on their duration and a time budget.
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

        allocation[test] = min(budget_per_test // duration, _MAX_TEST_RETRY_COUNT)

    return allocation
