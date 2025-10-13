import datetime
import os
import platform
import sys
import typing
from collections.abc import Mapping

import _pytest.config
import _pytest.config.argparsing
import _pytest.main
import _pytest.nodes
import _pytest.pathlib
import _pytest.reports
import _pytest.runner
import _pytest.terminal
import opentelemetry.trace
import pytest
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from pytest_mergify import flaky_detection, utils
from pytest_mergify.ci_insights import MergifyCIInsights


class PytestMergify:
    mergify_ci: MergifyCIInsights

    _flaky_detection_deadline: typing.Optional[datetime.datetime] = None
    _last_item: typing.Optional[_pytest.nodes.Item] = None

    def pytest_configure(self, config: _pytest.config.Config) -> None:
        kwargs = {}
        api_url = config.getoption("--mergify-api-url")
        if api_url is not None:
            kwargs["api_url"] = api_url
        self.mergify_ci = MergifyCIInsights(**kwargs)

    def pytest_terminal_summary(
        self, terminalreporter: _pytest.terminal.TerminalReporter
    ) -> None:
        # No CI, nothing to do
        if not utils.is_in_ci():
            return

        terminalreporter.section("Mergify CI")

        if not self.mergify_ci.token:
            terminalreporter.write_line(
                "No token configured for Mergify; test results will not be uploaded",
                yellow=True,
            )
            return

        if not self.mergify_ci.repo_name:
            terminalreporter.write_line(
                "Unable to determine repository name; test results will not be uploaded",
                red=True,
            )
            return

        if self.mergify_ci.flaky_detector:
            terminalreporter.write_line(self.mergify_ci.flaky_detector.make_report())
        elif self.mergify_ci.flaky_detector_error_message:
            terminalreporter.write_line(
                f"""⚠️  Flaky detection couldn't be enabled because of an error.

Common issues:
  • Your 'MERGIFY_TOKEN' might not be set or could be invalid
  • There might be a network connectivity issue with the Mergify API

📚 Documentation: https://docs.mergify.com/ci-insights/test-frameworks/pytest/
🔍 Details: {self.mergify_ci.flaky_detector_error_message}""",
                yellow=True,
            )

        # CI Insights Quarantine warning logs
        if not self.mergify_ci.branch_name:
            terminalreporter.write_line(
                "No valid branch name found, unable to setup CI Insights Quarantine",
                yellow=True,
            )

        if self.mergify_ci.quarantined_tests is None:
            terminalreporter.write_line("CI Insights Quarantine could not be setup")
        elif self.mergify_ci.quarantined_tests is not None:
            if self.mergify_ci.quarantined_tests.init_error_msg:
                terminalreporter.write_line(
                    self.mergify_ci.quarantined_tests.init_error_msg, yellow=True
                )
            else:
                terminalreporter.write_line(
                    self.mergify_ci.quarantined_tests.quarantined_tests_report()
                )

        # CI Insights Traces upload logs
        if self.mergify_ci.tracer_provider is None:
            terminalreporter.write_line(
                "Mergify Tracer didn't start for unexpected reason (please contact Mergify support); test results will not be uploaded",
                red=True,
            )
        else:
            try:
                self.mergify_ci.tracer_provider.force_flush()
            except Exception as e:
                terminalreporter.write_line(
                    f"Error while exporting traces: {e}",
                    red=True,
                )
            else:
                terminalreporter.write_line(
                    f"MERGIFY_TEST_RUN_ID={self.mergify_ci.test_run_id}",
                )

            try:
                self.mergify_ci.tracer_provider.shutdown()
            except Exception as e:
                terminalreporter.write_line(
                    f"Error while shutting down the tracer: {e}",
                    red=True,
                )

    @property
    def tracer(self) -> typing.Optional[opentelemetry.trace.Tracer]:
        return self.mergify_ci.tracer

    def pytest_sessionstart(self, session: _pytest.main.Session) -> None:
        if self.tracer:
            traceparent = os.environ.get("MERGIFY_TRACEPARENT")
            if traceparent:
                ctx = TraceContextTextMapPropagator().extract(
                    carrier={"traceparent": traceparent}
                )

            self.session_span = self.tracer.start_span(
                "pytest session start",
                attributes={
                    "test.scope": "session",
                },
                context=ctx if traceparent else None,
            )
        self.has_error = False

        self._last_item = None

    def pytest_collection_finish(self, session: _pytest.main.Session) -> None:
        # Record the last originally collected item so we can safely enqueue
        # flaky-detection retries right before and after it runs.
        if len(session.items) > 0:
            self._last_item = session.items[-1]

    @pytest.hookimpl(hookwrapper=True)
    def pytest_sessionfinish(
        self,
        session: _pytest.main.Session,
    ) -> typing.Generator[None, None, None]:
        if not self.tracer:
            yield
            return

        yield

        self.session_span.set_status(
            opentelemetry.trace.StatusCode.ERROR
            if self.has_error
            else opentelemetry.trace.StatusCode.OK
        )
        self.session_span.end()

    def _attributes_from_item(
        self, item: _pytest.nodes.Item
    ) -> typing.Dict[str, typing.Union[str, int]]:
        filepath, line_number, testname = item.location
        namespace = testname.replace(item.name, "")
        if namespace.endswith("."):
            namespace = namespace[:-1]

        return {
            SpanAttributes.CODE_FILEPATH: filepath,
            SpanAttributes.CODE_FUNCTION: item.name,
            SpanAttributes.CODE_LINENO: line_number or 0,
            SpanAttributes.CODE_NAMESPACE: namespace,
            "code.file.path": str(_pytest.pathlib.absolutepath(item.reportinfo()[0])),
            "code.line.number": line_number or 0,
        }

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(
        self, item: _pytest.nodes.Item
    ) -> typing.Generator[None, None, None]:
        if not self.tracer:
            yield
            return

        additional_attributes: typing.Dict[str, typing.Any] = {}
        if _should_skip_item(item):
            additional_attributes["test.case.result.status"] = "skipped"
            additional_attributes["cicd.test.quarantined"] = False
        else:
            quarantined = self.mergify_ci.mark_test_as_quarantined_if_needed(item)
            additional_attributes["cicd.test.quarantined"] = quarantined

        context = opentelemetry.trace.set_span_in_context(self.session_span)
        with self.tracer.start_as_current_span(
            item.nodeid,
            attributes={
                **self._attributes_from_item(item),
                **additional_attributes,
                **{"test.scope": "case"},
            },
            context=context,
        ):
            yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(
        self,
        item: _pytest.nodes.Item,
        nextitem: typing.Optional[_pytest.nodes.Item],
    ) -> typing.Generator[None, None, None]:
        if not self.mergify_ci.flaky_detector:
            yield
            return

        is_flaky_detection_item = item.keywords.get(
            flaky_detection.FLAKY_DETECTION_MARKER,
        )
        if is_flaky_detection_item:
            # Protect the budget allocated to flaky detection. If tests take
            # longer than expected, we clear the remaining items.
            is_deadline_exceeded = (
                self._flaky_detection_deadline
                and datetime.datetime.now(datetime.timezone.utc)
                > self._flaky_detection_deadline
            )
            if is_deadline_exceeded:
                item.session.items.clear()

            yield
            return

        # Before we run the last item, append the items generated by the flaky
        # detector so this test's teardown still sees a next test. Otherwise
        # pytest considers the session finished, tears down session-scoped
        # fixtures, and our end-of-queue retries won't run.
        if nextitem and self._last_item and nextitem.nodeid == self._last_item.nodeid:
            item.session.items.extend(
                self.mergify_ci.flaky_detector.get_remaining_items(
                    item.session,
                )
            )
            self._flaky_detection_deadline = (
                self.mergify_ci.flaky_detector.get_budget_deadline()
            )

        yield

        # After we run the last item, we know how long it took. If that unlocks
        # more budget for the flaky detection, use it now.
        if not nextitem:
            item.session.items.extend(
                self.mergify_ci.flaky_detector.get_remaining_items(
                    item.session,
                )
            )

    def pytest_exception_interact(
        self,
        node: _pytest.nodes.Node,
        call: _pytest.runner.CallInfo[typing.Any],
        report: _pytest.reports.TestReport,
    ) -> None:
        if self.tracer is None:
            return

        excinfo = call.excinfo

        if excinfo is not None:
            test_span = opentelemetry.trace.get_current_span()

            test_span.set_attributes(
                {
                    SpanAttributes.EXCEPTION_TYPE: str(excinfo.type.__name__),
                    SpanAttributes.EXCEPTION_MESSAGE: str(excinfo.value),
                    SpanAttributes.EXCEPTION_STACKTRACE: str(report.longrepr),
                }
            )
            test_span.set_status(
                opentelemetry.trace.Status(
                    status_code=opentelemetry.trace.StatusCode.ERROR,
                    description=f"{excinfo.type}: {excinfo.value}",
                )
            )

    def pytest_runtest_logreport(self, report: _pytest.reports.TestReport) -> None:
        if self.tracer is None:
            return

        if report.when != "call":
            return

        if report.outcome is None:
            return  # type: ignore[unreachable]

        has_error = report.outcome == "failed"
        status_code = (
            opentelemetry.trace.StatusCode.ERROR
            if has_error
            else opentelemetry.trace.StatusCode.OK
        )
        self.has_error |= has_error

        test_span = opentelemetry.trace.get_current_span()
        test_span.set_status(status_code)
        test_span.set_attributes(
            {
                "test.case.result.status": report.outcome,
            }
        )

        if self.mergify_ci.flaky_detector:
            detected = self.mergify_ci.flaky_detector.detect_from_report(report)
            if detected:
                opentelemetry.trace.get_current_span().set_attributes(
                    {"cicd.test.new": True}
                )


def pytest_addoption(parser: _pytest.config.argparsing.Parser) -> None:
    group = parser.getgroup("pytest-mergify", "Mergify support for pytest")
    group.addoption(
        "--mergify-api-url",
        help=(
            "URL of the Mergify API (or set via MERGIFY_API_URL environment variable)"
        ),
    )


def pytest_configure(config: _pytest.config.Config) -> None:
    # NOTE(remyduthu):
    # We are using `isinstance` instead of `get_plugin` because the plugin can
    # be registered with a different name (e.g. `pytester`). It feels safer to
    # check the class name directly.
    for plugin in config.pluginmanager.get_plugins():
        if isinstance(plugin, PytestMergify):
            return

    config.pluginmanager.register(PytestMergify(), name="PytestMergify")


def _should_skip_item(item: _pytest.nodes.Item) -> bool:
    if item.get_closest_marker("skip") is not None:
        return True

    skipif_marker = item.get_closest_marker("skipif")
    if skipif_marker is None:
        return False

    condition = skipif_marker.args[0]
    if not isinstance(condition, str):
        return bool(condition)

    # Mimics how pytest evaluate the conditions
    # https://github.com/pytest-dev/pytest/blob/c5a75f2498c86850c4ce13bcf10d56efc92394a4/src/_pytest/skipping.py#L88
    globals_ = {
        "os": os,
        "sys": sys,
        "platform": platform,
        "config": item.config,
    }
    if hasattr(item, "ihook"):
        for dictionary in reversed(
            item.ihook.pytest_markeval_namespace(config=item.config)
        ):
            if not isinstance(dictionary, Mapping):
                raise ValueError(
                    f"pytest_markeval_namespace() needs to return a dict, got {dictionary!r}"
                )
            globals_.update(dictionary)
    if hasattr(item, "obj"):
        globals_.update(item.obj.__globals__)

    condition_code = compile(
        source=condition,
        filename=f"<{skipif_marker.name} condition>",
        mode="eval",
    )

    # nosemgrep: python.lang.security.audit.eval-detected.eval-detected
    return bool(eval(condition_code, globals_))
