import _pytest.pytester
import _pytest.config


from tests import conftest


def test_span_resources_attributes_ci(
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
            assert spans[0].resource.attributes["cicd.provider.name"] == utils.get_ci_provider()
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_span_resources_attributes_pytest(
    pytester: _pytest.pytester.Pytester,
    reconfigure_mergify_tracer: conftest.ReconfigureT,
) -> None:
    reconfigure_mergify_tracer({"_PYTEST_MERGIFY_TEST": "true"})
    pytester.makepyfile(
        """
        import re

        import pytest

        def test_span(pytestconfig):
            plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
            assert plugin is not None
            assert plugin.mergify_tracer.exporter is not None
            spans = plugin.mergify_tracer.exporter.get_finished_spans()
            assert spans[0].resource.attributes["test.framework"] == "pytest"
            assert re.match(r"\d\.", spans[0].resource.attributes["test.framework.version"])
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_span_github_actions(
    pytester: _pytest.pytester.Pytester,
    reconfigure_mergify_tracer: conftest.ReconfigureT,
) -> None:
    # Do a partial reconfig, half GHA, half local to have spans
    reconfigure_mergify_tracer(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "Mergifyio/pytest-mergify",
            "_PYTEST_MERGIFY_TEST": "true",
        },
    )
    pytester.makepyfile(
        """
        import pytest

        from pytest_mergify import utils

        def test_span(pytestconfig):
            plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
            assert plugin is not None
            assert plugin.mergify_tracer.exporter is not None
            spans = plugin.mergify_tracer.exporter.get_finished_spans()
            assert spans[0].resource.attributes["vcs.repository.name"] == "Mergifyio/pytest-mergify"
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
