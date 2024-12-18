import _pytest.pytester
import _pytest.config

from tests import conftest


def test_span(
    pytester: _pytest.pytester.Pytester,
    reconfigure_mergify_tracer: conftest.ReconfigureT,
) -> None:
    reconfigure_mergify_tracer({"_PYTEST_MERGIFY_TEST": "true"})
    pytester.makepyfile(
        """
        import pytest

        from pytest_mergify import utils

        def test_span(pytestconfig):
            plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
            assert plugin is not None
            assert plugin.mergify_tracer.exporter is not None
            spans = plugin.mergify_tracer.exporter.get_finished_spans()
            assert any(s.name == "pytestconfig setup" for s in spans)
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
