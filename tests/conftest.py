import typing
import http.server
import socketserver
import threading

import pytest
import _pytest.pytester
from opentelemetry.sdk import trace

import pytest_mergify

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace import ReadableSpan

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def set_api_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Always override API
    monkeypatch.setenv("MERGIFY_API_URL", "http://localhost:9999")


PytesterWithSpanReturnT = typing.Tuple[
    _pytest.pytester.RunResult, typing.Optional[typing.Dict[str, trace.ReadableSpan]]
]


class PytesterWithSpanT(typing.Protocol):
    def __call__(
        self,
        code: str = ...,
        setenv: typing.Optional[typing.Dict[str, typing.Optional[str]]] = ...,
    ) -> PytesterWithSpanReturnT: ...


_DEFAULT_PYTESTER_CODE = "def test_pass(): pass"


@pytest.fixture
def pytester_with_spans(
    pytester: _pytest.pytester.Pytester, monkeypatch: pytest.MonkeyPatch
) -> PytesterWithSpanT:
    def _run(
        code: str = _DEFAULT_PYTESTER_CODE,
        setenv: typing.Optional[typing.Dict[str, typing.Optional[str]]] = None,
    ) -> PytesterWithSpanReturnT:
        monkeypatch.delenv("PYTEST_MERGIFY_DEBUG", raising=False)
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("_PYTEST_MERGIFY_TEST", "true")
        for k, v in (setenv or {}).items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        plugin = pytest_mergify.PytestMergify()
        pytester.makepyfile(code)
        result = pytester.runpytest_inprocess(plugins=[plugin])

        spans_as_dict: typing.Optional[typing.Dict[str, ReadableSpan]]
        if code is _DEFAULT_PYTESTER_CODE:
            result.assert_outcomes(passed=1)
        if isinstance(plugin.mergify_tracer.exporter, InMemorySpanExporter):
            spans = plugin.mergify_tracer.exporter.get_finished_spans()
            spans_as_dict = {}
            for span in spans:
                if not span.attributes or not span.attributes.get("code.namespace"):
                    span_name = span.name
                else:
                    code_namespace = span.attributes["code.namespace"]
                    span_name = f"{code_namespace}.{span.name}"

                spans_as_dict[span_name] = span

            # Make sure we don't lose spans in the process
            assert len(spans_as_dict) == len(spans)
        else:
            spans_as_dict = None

        return result, spans_as_dict

    return _run


class TestHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    # Class attribute for the response code, set by the fixture.
    response_code: int = 200

    def do_POST(self) -> None:
        path = self.path[1:].split("/")
        # loozy match, who cares
        if path[0] == "v1" and path[-1] == "traces" and path[-2] == "ci":
            self.send_response(self.__class__.response_code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Override to suppress console logging during tests.
        pass


@pytest.fixture
def http_server(request: pytest.FixtureRequest) -> typing.Generator[str, None, None]:
    # Allow parameterization of the response code via request.param.
    response_code = getattr(request, "param", 200)
    TestHTTPRequestHandler.response_code = response_code

    with socketserver.TCPServer(("", 0), TestHTTPRequestHandler) as httpd:
        host, port = httpd.server_address  # retrieve the actual port
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        yield f"http://{host!s}:{port}"
        httpd.shutdown()
