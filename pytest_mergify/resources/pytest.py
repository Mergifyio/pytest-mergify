import pytest
from opentelemetry.sdk.resources import Resource, ResourceDetector


class PytestResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for GitHub Actions."""

    def detect(self) -> Resource:
        return Resource(
            {
                "test.framework": "pytest",
                "test.framework.version": pytest.__version__,
            }
        )
