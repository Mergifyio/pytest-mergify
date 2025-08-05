from opentelemetry.sdk.resources import Resource, ResourceDetector
from opentelemetry.semconv._incubating.attributes import cicd_attributes, vcs_attributes

from pytest_mergify import utils


class JenkinsResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for Jenkins."""

    OPENTELEMETRY_JENKINS_MAPPING = {
        cicd_attributes.CICD_PIPELINE_NAME: (str, "JOB_NAME"),
        cicd_attributes.CICD_PIPELINE_TASK_NAME: (str, "JOB_NAME"),
        cicd_attributes.CICD_PIPELINE_RUN_ID: (str, "BUILD_ID"),
        "cicd.pipeline.run.url": (str, "BUILD_URL"),
        "cicd.pipeline.runner.name": (str, "NODE_NAME"),
        vcs_attributes.VCS_REF_HEAD_NAME: (str, "GIT_BRANCH"),
        vcs_attributes.VCS_REF_HEAD_REVISION: (str, "GIT_COMMIT"),
        vcs_attributes.VCS_REPOSITORY_URL_FULL: (str, "GIT_URL"),
        "vcs.repository.name": (
            str,
            lambda: utils.get_repository_name_from_env_url("GIT_URL"),
        ),
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "jenkins":
            return Resource({})

        attributes = utils.get_attributes(self.OPENTELEMETRY_JENKINS_MAPPING)
        return Resource(attributes)
