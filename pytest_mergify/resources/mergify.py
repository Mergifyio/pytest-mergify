from opentelemetry.sdk.resources import Resource, ResourceDetector

from pytest_mergify import utils


class MergifyResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for Mergify fields."""

    OPENTELEMETRY_MERGIFY_MAPPING = {
        "mergify.test.job.name": (str, "MERGIFY_TEST_JOB_NAME"),
    }

    def detect(self) -> Resource:
        attributes = utils.get_attributes(self.OPENTELEMETRY_MERGIFY_MAPPING)
        return Resource(attributes)
