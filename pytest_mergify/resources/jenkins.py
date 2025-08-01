import os
from opentelemetry.sdk.resources import Resource, ResourceDetector

from opentelemetry.semconv._incubating.attributes import cicd_attributes
from opentelemetry.semconv._incubating.attributes import vcs_attributes

from pytest_mergify import utils


class RepsoitoryNameExtractionError(Exception):
    pass


class JenkinsResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for Jenkins."""

    OPENTELEMETRY_JENKINS_MAPPING = {
        cicd_attributes.CICD_PIPELINE_NAME: (str, "JOB_NAME"),
        cicd_attributes.CICD_PIPELINE_RUN_ID: (str, "BUILD_ID"),
        "cicd.pipeline.run.url": (str, "BUILD_URL"),
        "cicd.pipeline.runner.name": (str, "NODE_NAME"),
        vcs_attributes.VCS_REF_HEAD_NAME: (str, "GIT_BRANCH"),
        vcs_attributes.VCS_REF_HEAD_REVISION: (str, "GIT_COMMIT"),
        vcs_attributes.VCS_REPOSITORY_URL_FULL: (str, "GIT_URL"),
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "jenkins":
            return Resource({})

        attributes = {}
        for attribute_name, (
            type_,
            envvar,
        ) in self.OPENTELEMETRY_JENKINS_MAPPING.items():
            if envvar in os.environ:
                try:
                    attributes[attribute_name] = type_(os.environ[envvar])
                except Exception:
                    pass  # skip invalid conversions

        repository_name = utils._get_repository_name_from_env_url("GIT_URL")
        if repository_name:
            attributes["vcs.repository.name"] = repository_name

        return Resource(attributes)
