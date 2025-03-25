import os

from opentelemetry.sdk.resources import Resource, ResourceDetector


class MergifyResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for Mergify fields."""

    def detect(self) -> Resource:
        r = {}
        if "MERGIFY_TEST_JOB_NAME" in os.environ:
            r["mergify.test.job.name"] = os.environ["MERGIFY_TEST_JOB_NAME"]

        return Resource(r)
