from opentelemetry.sdk.resources import Resource, ResourceDetector

from opentelemetry.sdk.trace import id_generator


class MergifyResourceDetector(ResourceDetector):
    def detect(self) -> Resource:
        return Resource(
            {
                "test.run.id": id_generator.RandomIdGenerator().generate_span_id(),
            }
        )
