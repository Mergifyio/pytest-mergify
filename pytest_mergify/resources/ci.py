from opentelemetry.sdk.resources import Resource, ResourceDetector

from pytest_mergify import utils


class CIResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for GitHub Actions."""

    def detect(self) -> Resource:
        if (provider := utils.get_ci_provider()) is None:
            return Resource({})

        return Resource(
            {
                "cicd.provider.name": provider,
            }
        )
