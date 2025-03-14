import typing

import pytest
import _pytest.main
import _pytest.runner
import _pytest.reports
import _pytest.config
import _pytest.config.argparsing
import _pytest.nodes
import _pytest.terminal
import opentelemetry.trace
from opentelemetry.semconv.trace import SpanAttributes

from pytest_mergify import utils
from pytest_mergify.tracer import MergifyTracer


class PytestMergify:
    mergify_tracer: MergifyTracer

    def pytest_configure(self, config: _pytest.config.Config) -> None:
        kwargs = {}
        api_url = config.getoption("--mergify-api-url")
        if api_url is not None:
            kwargs["api_url"] = api_url
        self.mergify_tracer = MergifyTracer(**kwargs)

    def pytest_terminal_summary(
        self, terminalreporter: _pytest.terminal.TerminalReporter
    ) -> None:
        terminalreporter.section("Mergify CI")

        # Make sure we shutdown and flush traces before existing: this makes
        # sure that we capture the possible error logs, otherwise they are
        # emitted on exit (atexit()).
        if self.mergify_tracer.tracer_provider is not None:
            try:
                self.mergify_tracer.tracer_provider.force_flush()
            except Exception as e:
                terminalreporter.write_line(
                    f"Error while exporting traces: {e}",
                    red=True,
                )
            else:
                if utils.get_ci_provider() == "github_actions":
                    terminalreporter.write_line(
                        f"::notice title=Mergify CI::MERGIFY_TEST_RUN_ID={self.mergify_tracer.test_run_id}",
                    )

            try:
                self.mergify_tracer.tracer_provider.shutdown()
            except Exception as e:
                terminalreporter.write_line(
                    f"Error while shutting down the tracer: {e}",
                    red=True,
                )

        if self.mergify_tracer.token is None:
            terminalreporter.write_line(
                "No token configured for Mergify; test results will not be uploaded",
                yellow=True,
            )
            return

        if self.mergify_tracer.repo_name is None:
            terminalreporter.write_line(
                "Unable to determine repository name; test results will not be uploaded",
                red=True,
            )
            return

    @property
    def tracer(self) -> typing.Optional[opentelemetry.trace.Tracer]:
        return self.mergify_tracer.tracer

    def pytest_sessionstart(self, session: _pytest.main.Session) -> None:
        if self.tracer:
            self.session_span = self.tracer.start_span(
                "pytest session start",
                attributes={
                    "test.scope": "session",
                },
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
        filepath, line_number, _ = item.location
        return {
            SpanAttributes.CODE_FILEPATH: filepath,
            SpanAttributes.CODE_FUNCTION: item.name,
            SpanAttributes.CODE_LINENO: line_number or 0,
        }

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(
        self, item: _pytest.nodes.Item
    ) -> typing.Generator[None, None, None]:
        if self.tracer:
            context = opentelemetry.trace.set_span_in_context(self.session_span)
            with self.tracer.start_as_current_span(
                item.name,
                attributes={
                    **self._attributes_from_item(item),
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
            "URL of the Mergify API (or set via MERGIFY_API_URL environment variable)",
        ),
    )


def pytest_configure(config: _pytest.config.Config) -> None:
    config.pluginmanager.register(PytestMergify(), name="PytestMergify")
