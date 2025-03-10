import os
import pathlib
import json
import typing

from opentelemetry.sdk.resources import Resource, ResourceDetector

from opentelemetry.semconv._incubating.attributes import cicd_attributes
from opentelemetry.semconv._incubating.attributes import vcs_attributes

from pytest_mergify import utils


class GitHubActionsResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for GitHub Actions."""

    @staticmethod
    def get_github_actions_head_sha() -> typing.Optional[str]:
        if os.getenv("GITHUB_EVENT_NAME") == "pull_request":
            # NOTE: we want the head sha of the pull request
            event_raw_path = os.getenv("GITHUB_EVENT_PATH")
            if event_raw_path and (
                (event_path := pathlib.Path(event_raw_path)).is_file()
            ):
                event = json.loads(event_path.read_bytes())
                return str(event["pull_request"]["head"]["sha"])
        return os.getenv("GITHUB_SHA")

    @staticmethod
    def get_github_head_ref_name() -> typing.Optional[str]:
        return os.getenv("GITHUB_HEAD_REF") or os.getenv("GITHUB_REF")

    OPENTELEMETRY_GHA_MAPPING = {
        cicd_attributes.CICD_PIPELINE_NAME: (str, "GITHUB_JOB"),
        cicd_attributes.CICD_PIPELINE_RUN_ID: (int, "GITHUB_RUN_ID"),
        "cicd.pipeline.run.attempt": (int, "GITHUB_RUN_ATTEMPT"),
        cicd_attributes.CICD_PIPELINE_TASK_NAME: (str, "GITHUB_ACTION"),
        vcs_attributes.VCS_REF_HEAD_TYPE: (str, "GITHUB_REF_TYPE"),
        vcs_attributes.VCS_REF_BASE_NAME: (str, "GITHUB_BASE_REF"),
        "vcs.repository.name": (str, "GITHUB_REPOSITORY"),
        "vcs.repository.id": (int, "GITHUB_REPOSITORY_ID"),
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "github_actions":
            return Resource({})

        attributes = {}

        if "GITHUB_SERVER_URL" in os.environ and "GITHUB_REPOSITORY" in os.environ:
            attributes[vcs_attributes.VCS_REPOSITORY_URL_FULL] = (
                os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"]
            )

        head_ref = self.get_github_head_ref_name()
        if head_ref is not None:
            attributes[vcs_attributes.VCS_REF_HEAD_NAME] = head_ref

        head_sha = self.get_github_actions_head_sha()
        if head_sha is not None:
            attributes[vcs_attributes.VCS_REF_HEAD_REVISION] = head_sha

        for attribute_name, (type_, envvar) in self.OPENTELEMETRY_GHA_MAPPING.items():
            if envvar in os.environ:
                attributes[attribute_name] = type_(os.environ[envvar])

        return Resource(attributes)
