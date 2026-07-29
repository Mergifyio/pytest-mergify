import dataclasses
import datetime
import gzip
import http.server
import os
import re
import socketserver
import threading
import typing
import uuid

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

import _pytest.pytester
import pytest
import responses
from opentelemetry.sdk import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

import pytest_mergify
from pytest_mergify import utils

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
        quarantined_tests: typing.Optional[typing.List[str]] = None,
    ) -> PytesterWithSpanReturnT: ...


_DEFAULT_PYTESTER_CODE = "def test_pass(): pass"


@pytest.fixture
def pytester_with_spans(
    pytester: _pytest.pytester.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> PytesterWithSpanT:
    @responses.activate
    def _run(
        code: str = _DEFAULT_PYTESTER_CODE,
        setenv: typing.Optional[typing.Dict[str, typing.Optional[str]]] = None,
        quarantined_tests: typing.Optional[typing.List[str]] = None,
    ) -> PytesterWithSpanReturnT:
        monkeypatch.delenv("PYTEST_MERGIFY_DEBUG", raising=False)
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("_PYTEST_MERGIFY_TEST", "true")

        for k, v in (setenv or {}).items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)

        api_url = os.getenv("MERGIFY_API_URL")

        qtest_resp: typing.Dict[str, typing.Any]
        if not quarantined_tests:
            qtest_resp = {"quarantined_tests": []}
        else:
            qtest_resp = {
                "quarantined_tests": [
                    {
                        "id": uuid.uuid4().hex,
                        "test_name": qtest,
                        "reason": "reasonfoobar",
                        "branch": None,
                        "created_at": datetime.datetime.now().isoformat(),
                    }
                    for qtest in quarantined_tests
                ]
            }

        responses.add(
            responses.GET,
            re.compile(rf"{api_url}/v1/ci/.*/repositories/.*/quarantines\?branch=.*"),
            status=200,
            json=qtest_resp,
        )

        full_repository = utils.get_repository_name()
        if full_repository is not None:
            try:
                owner, repo = utils.split_full_repo_name(full_repository)
            except utils.InvalidRepositoryFullNameError:
                pass
            else:
                passthrough = responses.Response(
                    responses.POST,
                    f"{api_url}/v1/ci/{owner}/repositories/{repo}/traces",
                    passthrough=True,
                )
                responses.add(passthrough)

        plugin = pytest_mergify.PytestMergify()
        pytester.makepyfile(code)
        result = pytester.runpytest_inprocess(plugins=[plugin])

        spans_as_dict: typing.Optional[typing.Dict[str, ReadableSpan]]
        if code is _DEFAULT_PYTESTER_CODE:
            result.assert_outcomes(passed=1)
        if isinstance(plugin.mergify_ci.exporter, InMemorySpanExporter):
            spans = plugin.mergify_ci.exporter.get_finished_spans()
            spans_as_dict = {span.name: span for span in spans}
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
        if path[0] == "v1" and path[-1] == "traces":
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


def _decode_any_value(value: typing.Any) -> typing.Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None

    if kind == "array_value":
        return [_decode_any_value(item) for item in value.array_value.values]

    return getattr(value, kind)


def _decode_attributes(key_values: typing.Any) -> typing.Dict[str, typing.Any]:
    return {kv.key: _decode_any_value(kv.value) for kv in key_values}


@dataclasses.dataclass
class UploadedSpan:
    name: str
    attributes: typing.Dict[str, typing.Any]


@dataclasses.dataclass
class UploadedBatch:
    """
    One resource's spans, as they arrived over the wire.

    A request carries a batch per resource it saw, so counting these counts
    resources rather than requests -- which for this plugin, holding one
    provider for the whole session, comes to one per request.
    """

    resource_attributes: typing.Dict[str, typing.Any]
    spans: typing.List[UploadedSpan]

    def span(self, name: str) -> UploadedSpan:
        """
        The one span with this name.

        A list rather than a dict keyed by name, because names repeat: a rerun
        of a flaky test uploads the same node id again. Unpacking raises here
        instead of letting the second copy overwrite the first unseen.
        """
        (span,) = [span for span in self.spans if span.name == name]
        return span


class _OTLPServer(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.bodies: typing.List[bytes] = []
        super().__init__(*args, **kwargs)


class _OTLPRequestHandler(http.server.BaseHTTPRequestHandler):
    server: _OTLPServer

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.server.bodies.append(body)

        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def do_GET(self) -> None:
        # Quarantine and test selection share this base URL. Answering 404 keeps
        # them out of the way without pretending they were served.
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@dataclasses.dataclass
class OTLPCollector:
    """
    What the plugin actually put on the wire.

    The in-memory exporter reaches into the plugin's own process, which cannot
    see a batch that was never sent, a process other than this one, or a run
    that ended without a terminal summary. This can.
    """

    url: str
    _server: _OTLPServer

    @property
    def batches(self) -> typing.List[UploadedBatch]:
        batches = []

        for body in self._server.bodies:
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)

            for resource_spans in request.resource_spans:
                spans = [
                    UploadedSpan(
                        name=span.name,
                        attributes=_decode_attributes(span.attributes),
                    )
                    for scope_spans in resource_spans.scope_spans
                    for span in scope_spans.spans
                ]
                batches.append(
                    UploadedBatch(
                        resource_attributes=_decode_attributes(
                            resource_spans.resource.attributes
                        ),
                        spans=spans,
                    )
                )

        return batches

    @property
    def span_names(self) -> typing.Set[str]:
        return {span.name for batch in self.batches for span in batch.spans}


@pytest.fixture
def otlp_collector() -> typing.Generator[OTLPCollector, None, None]:
    with _OTLPServer(("127.0.0.1", 0), _OTLPRequestHandler) as httpd:
        host, port = httpd.server_address[0], httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()

        yield OTLPCollector(url=f"http://{host!s}:{port}", _server=httpd)

        httpd.shutdown()


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
