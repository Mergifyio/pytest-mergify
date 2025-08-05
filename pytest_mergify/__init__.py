import typing

import os
import sys
from collections.abc import Mapping
import platform
import pytest
import _pytest.main
import _pytest.runner
import _pytest.reports
import _pytest.config
import _pytest.config.argparsing
import _pytest.pathlib
import _pytest.nodes
import _pytest.terminal
import opentelemetry.trace
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from pytest_mergify import utils
from pytest_mergify.ci_insights import MergifyCIInsights


class PytestMergify:
    mergify_ci: MergifyCIInsights

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

        # CI Insights Quarantine warning logs
        if not self.mergify_ci.branch_name:
            terminalreporter.write_line(
                "No valid branch name found, unable to setup CI Insights Quarantine",
                yellow=True,
            )

        if (
            self.mergify_ci.quarantined_tests is not None
            and self.mergify_ci.quarantined_tests.init_error_msg
        ):
            terminalreporter.write_line(
                self.mergify_ci.quarantined_tests.init_error_msg, yellow=True
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

    def pytest_sessionfinish(self, session: _pytest.main.Session) -> None:
        if self.tracer:
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
        if self.tracer:
            if item.get_closest_marker("skip") is not None:
                skip = True
            elif (skipif_marker := item.get_closest_marker("skipif")) is not None:
                condition = skipif_marker.args[0]
                if isinstance(condition, str):
                    #  Mimics how pytest evaluate the conditions
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
                    filename = f"<{skipif_marker.name} condition>"
                    condition_code = compile(condition, filename, "eval")
                    # nosemgrep: python.lang.security.audit.eval-detected.eval-detected
                    skip = eval(condition_code, globals_)
                else:
                    skip = bool(condition)
            else:
                skip = False

            if skip:
                skip_attributes = {"test.case.result.status": "skipped"}
            else:
                skip_attributes = {}

                self.mergify_ci.mark_test_as_quarantined_if_needed(item)

            context = opentelemetry.trace.set_span_in_context(self.session_span)
            with self.tracer.start_as_current_span(
                item.nodeid,
                attributes={
                    **self._attributes_from_item(item),
                    **skip_attributes,
                    **{"test.scope": "case"},
                },
                context=context,
            ):
                yield
        else:
            yield

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
                    SpanAttributes.EXCEPTION_TYPE: str(excinfo.type),
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


def pytest_addoption(parser: _pytest.config.argparsing.Parser) -> None:
    group = parser.getgroup("pytest-mergify", "Mergify support for pytest")
    group.addoption(
        "--mergify-api-url",
        help=(
            "URL of the Mergify API (or set via MERGIFY_API_URL environment variable)"
        ),
    )


def pytest_configure(config: _pytest.config.Config) -> None:
    config.pluginmanager.register(PytestMergify(), name="PytestMergify")
