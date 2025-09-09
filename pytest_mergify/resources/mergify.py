import os

from opentelemetry.sdk.resources import Resource, ResourceDetector

from pytest_mergify import utils


class MergifyResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for Mergify fields."""

    OPENTELEMETRY_MERGIFY_MAPPING = {
        "mergify.test.job.name": (str, "MERGIFY_TEST_JOB_NAME"),
    }

    def detect(self) -> Resource:
        attributes = utils.get_attributes(self.OPENTELEMETRY_MERGIFY_MAPPING)

        if _is_flaky_test_detection_enabled():
            attributes["mergify.test.flaky_detection_enabled"] = True

        return Resource(attributes)


def _is_flaky_test_detection_enabled() -> bool:
    return os.getenv("MERGIFY_TEST_FLAKY_DETECTION", default="").lower() in {
        "y",
        "yes",
        "t",
        "true",
        "on",
        "1",
    }
