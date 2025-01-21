import os
import pathlib
import json

from opentelemetry.sdk.resources import Resource, ResourceDetector

from opentelemetry.semconv._incubating.attributes import cicd_attributes
from opentelemetry.semconv._incubating.attributes import vcs_attributes

from pytest_mergify import utils


class GitHubActionsResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for GitHub Actions."""

    @staticmethod
    def get_github_actions_head_sha() -> str | None:
        if os.getenv("GITHUB_EVENT_NAME") == "pull_request":
            # NOTE: we want the head sha of the pull request
            event_raw_path = os.getenv("GITHUB_EVENT_PATH")
            if event_raw_path and (
                (event_path := pathlib.Path(event_raw_path)).is_file()
            ):
                event = json.loads(event_path.read_bytes())
                return str(event["pull_request"]["head"]["sha"])
        return os.getenv("GITHUB_SHA")

    OPENTELEMETRY_GHA_MAPPING = {
        cicd_attributes.CICD_PIPELINE_NAME: "GITHUB_JOB",
        cicd_attributes.CICD_PIPELINE_RUN_ID: "GITHUB_RUN_ID",
        cicd_attributes.CICD_PIPELINE_TASK_NAME: "GITHUB_ACTION",
        vcs_attributes.VCS_REF_HEAD_NAME: "GITHUB_REF_NAME",
        vcs_attributes.VCS_REF_HEAD_TYPE: "GITHUB_REF_TYPE",
        vcs_attributes.VCS_REF_BASE_NAME: "GITHUB_BASE_REF",
        "vcs.repository.name": "GITHUB_REPOSITORY",
        "vcs.repository.id": "GITHUB_REPOSITORY_ID",
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "github_actions":
            return Resource({})

        attributes = {}

        if "GITHUB_SERVER_URL" in os.environ and "GITHUB_REPOSITORY" in os.environ:
            attributes[vcs_attributes.VCS_REPOSITORY_URL_FULL] = (
                os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"]
            )

        head_sha = self.get_github_actions_head_sha()
        if head_sha is not None:
            attributes[vcs_attributes.VCS_REF_HEAD_REVISION] = head_sha

        for attribute_name, envvar in self.OPENTELEMETRY_GHA_MAPPING.items():
            if envvar in os.environ:
                attributes[attribute_name] = os.environ[envvar]

        return Resource(attributes)
