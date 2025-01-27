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
            try:
                self.mergify_tracer.tracer_provider.shutdown()  # type: ignore[no-untyped-call]
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

        if self.mergify_tracer.interceptor is None:
            terminalreporter.write_line("Nothing to do")
        else:
            if self.mergify_tracer.interceptor.trace_id is None:
                terminalreporter.write_line(
                    "No trace id detected, this test run will not be attached to the CI job",
                    yellow=True,
                )
            elif utils.get_ci_provider() == "github_actions":
                terminalreporter.write_line(
                    f"::notice title=Mergify CI::MERGIFY_TRACE_ID={self.mergify_tracer.interceptor.trace_id}",
                )

    @property
    def tracer(self) -> opentelemetry.trace.Tracer | None:
        return self.mergify_tracer.tracer

    def pytest_sessionstart(self, session: _pytest.main.Session) -> None:
        if self.tracer:
            self.session_span = self.tracer.start_span(
                "pytest session start",
                attributes={
                    "test.type": "session",
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

    def _attributes_from_item(self, item: _pytest.nodes.Item) -> dict[str, str | int]:
        filepath, line_number, _ = item.location
        return {
            SpanAttributes.CODE_FILEPATH: filepath,
            SpanAttributes.CODE_FUNCTION: item.name,
            SpanAttributes.CODE_LINENO: line_number or 0,
            "test.case.name": item.nodeid,
        }

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(
        self, item: _pytest.nodes.Item
    ) -> typing.Generator[None, None, None]:
        if self.tracer:
            context = opentelemetry.trace.set_span_in_context(self.session_span)
            with self.tracer.start_as_current_span(
                item.nodeid,
                attributes=self._attributes_from_item(item) | {"test.type": "case"},
                context=context,
            ):
                yield
        else:
            yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(
        self, item: _pytest.nodes.Item
    ) -> typing.Generator[None, None, None]:
        if self.tracer:
            # Since there is no pytest_fixture_teardown hook, we have to be a
            # little clever to capture the spans for each fixture's teardown.
            # The pytest_fixture_post_finalizer hook is called at the end of a
            # fixture's teardown, but we don't know when the fixture actually
            # began tearing down.
            #
            # Instead start a span here for the first fixture to be torn down,
            # but give it a temporary name, since we don't know which fixture it
            # will be. Then, in pytest_fixture_post_finalizer, when we do know
            # which fixture is being torn down, update the name and attributes
            # to the actual fixture, end the span, and create the span for the
            # next fixture in line to be torn down.
            self._fixture_teardown_span = self.tracer.start_span("fixture teardown")
            yield
            # The last call to pytest_fixture_post_finalizer will create
            # a span that is unneeded, so delete it.
            del self._fixture_teardown_span
        else:
            yield

    def _attributes_from_fixturedef(
        self, fixturedef: _pytest.fixtures.FixtureDef[typing.Any]
    ) -> dict[str, str | int]:
        return {
            SpanAttributes.CODE_FILEPATH: fixturedef.func.__code__.co_filename,
            SpanAttributes.CODE_FUNCTION: fixturedef.argname,
            SpanAttributes.CODE_LINENO: fixturedef.func.__code__.co_firstlineno,
            "test.fixture.scope": fixturedef.scope,
            "test.type": "fixture",
        }

    def _name_from_fixturedef(
        self,
        fixturedef: _pytest.fixtures.FixtureDef[typing.Any],
        request: _pytest.fixtures.FixtureRequest,
    ) -> str:
        if fixturedef.params and "request" in fixturedef.argnames:
            try:
                parameter = str(request.param)
            except Exception:
                parameter = str(
                    request.param_index
                    if isinstance(request, _pytest.fixtures.SubRequest)
                    else "?"
                )
            return f"{fixturedef.argname}[{parameter}]"
        return fixturedef.argname

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(
        self,
        fixturedef: _pytest.fixtures.FixtureDef[typing.Any],
        request: _pytest.fixtures.FixtureRequest,
    ) -> typing.Generator[None, None, None]:
        if self.tracer:
            with self.tracer.start_as_current_span(
                name=f"{self._name_from_fixturedef(fixturedef, request)} setup",
                attributes=self._attributes_from_fixturedef(fixturedef),
            ):
                yield
        else:
            yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_post_finalizer(
        self,
        fixturedef: _pytest.fixtures.FixtureDef[typing.Any],
        request: _pytest.fixtures.SubRequest,
    ) -> typing.Generator[None, None, None]:
        """When the span for a fixture teardown is created by
        pytest_runtest_teardown or a previous pytest_fixture_post_finalizer, we
        need to update the name and attributes now that we know which fixture it
        was for."""

        if self.tracer:
            # If the fixture has already been torn down, then it will have no cached
            # result, so we can skip this one.
            if fixturedef.cached_result is None:
                yield
            # Passing `-x` option to pytest can cause it to exit early so it may not
            # have this span attribute.
            elif not hasattr(self, "_fixture_teardown_span"):  # pragma: no cover
                yield
            else:
                # If we've gotten here, we have a real fixture about to be torn down.
                name = f"{self._name_from_fixturedef(fixturedef, request)} teardown"
                self._fixture_teardown_span.update_name(name)
                attributes = self._attributes_from_fixturedef(fixturedef)
                self._fixture_teardown_span.set_attributes(
                    attributes  # type: ignore[arg-type]
                )
                yield
                self._fixture_teardown_span.end()

            # Create the span for the next fixture to be torn down. When there are
            # no more fixtures remaining, this will be an empty, useless span, so it
            # needs to be deleted by pytest_runtest_teardown.
            self._fixture_teardown_span = self.tracer.start_span("fixture teardown")
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
