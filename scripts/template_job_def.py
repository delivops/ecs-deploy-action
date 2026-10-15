#!/usr/bin/env python3
"""
Build the AWS Batch job definition from a Terraform-managed template (task_definition_source: template with
deployment_type: batch_job_definition).

Terraform keeps a complete "template" job definition under its own name and publishes that name to SSM:

  /batch/<task_name>/job-definition-template   the template job definition name

This script copies the template's latest ACTIVE revision, sets `jobDefinitionName` to `task_name`, replaces
`containerProperties.image`, removes the read-only fields and the tags, and writes job-definition.json for the
register step. Nothing else changes: command, env, secrets, roles, resources, timeout, retry strategy,
parameters and logging all come from the template.

Only single-container job definitions (`containerProperties`) are supported.

It emits `deregister_previous` (the action input) for the register step, which in YAML mode comes from the YAML.
"""

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict

import generate_job_def as gjd
import generate_task_def as gtd

# Fields DescribeJobDefinitions returns that RegisterJobDefinition doesn't take (per the botocore Batch model),
# plus the tags: a new revision isn't tagged, as in ECS template mode.
READ_ONLY_FIELDS = (
    "jobDefinitionArn",
    "revision",
    "status",
    "containerOrchestrationType",
    "tags",
)

# The job shapes other than a single container. Each is its own job definition structure with its own image
# location(s), so copying one would need a different image swap.
UNSUPPORTED_PROPERTIES = ("ecsProperties", "eksProperties", "nodeProperties")


class TemplateError(Exception):
    """A template that can't be deployed, or AWS access that failed; the message says what to fix."""


def ssm_name(job_name: str) -> str:
    return f"/batch/{job_name}/job-definition-template"


# --------------------------------------------------------------------------
# Pure core
# --------------------------------------------------------------------------

def render_from_template(template: Dict[str, Any], *, job_name: str, image: str) -> Dict[str, Any]:
    """Return a copy of `template` ready to register as `job_name`, with its image replaced."""
    name = template.get("jobDefinitionName")
    present = [p for p in UNSUPPORTED_PROPERTIES if template.get(p)]
    if present:
        raise TemplateError(
            f"Template job definition {name!r} uses {', '.join(present)}; template mode supports single-container "
            f"job definitions (containerProperties) only."
        )
    if template.get("type") != "container" or not isinstance(template.get("containerProperties"), dict):
        raise TemplateError(
            f"Template job definition {name!r} has type {template.get('type')!r} and no containerProperties; "
            f"template mode needs type 'container' with containerProperties."
        )

    job_def = copy.deepcopy(template)
    for field in READ_ONLY_FIELDS:
        job_def.pop(field, None)
    job_def["jobDefinitionName"] = job_name
    job_def["containerProperties"]["image"] = image
    return job_def


# --------------------------------------------------------------------------
# AWS layer (clients are passed in; boto3 is imported only in main())
# --------------------------------------------------------------------------

def _client_error(error, action: str, resource: str) -> TemplateError:
    """Translate a botocore ClientError into an actionable message (the style of template_task_def)."""
    code = error.response.get("Error", {}).get("Code", "Unknown")
    if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
        return TemplateError(f"Access denied: the deploy role needs {action} on {resource}.")
    if code in ("ThrottlingException", "TooManyUpdates", "RequestLimitExceeded"):
        return TemplateError(f"AWS throttled {action} ({code}) on {resource}. Retry the deploy.")
    if code in ("ExpiredTokenException", "ExpiredToken", "UnrecognizedClientException", "InvalidClientTokenId"):
        return TemplateError(f"AWS credentials are expired or invalid ({code}) while calling {action}.")
    return TemplateError(f"AWS error {code} from {action} on {resource}: {error}")


def read_template_name(ssm, job_name: str) -> str:
    from botocore.exceptions import ClientError

    name = ssm_name(job_name)
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"].strip()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
            raise TemplateError(
                f"SSM parameter {name} not found. Template mode needs the Terraform that manages the template job "
                f"definition to publish its name there (see docs/template.md)."
            ) from e
        raise _client_error(e, "ssm:GetParameter", f"parameter {name}") from e
    if not value:
        raise TemplateError(f"SSM parameter {name} is empty; it should hold the template job definition name.")
    if not gjd.JOB_DEFINITION_NAME_PATTERN.match(value):
        raise TemplateError(
            f"SSM parameter {name} holds {value!r}; it should hold the template job definition's name (letters, "
            f"digits, '-' and '_'), not an ARN or a name:revision."
        )
    if value == job_name:
        raise TemplateError(
            f"SSM parameter {name} names the job definition itself ({value!r}); the template must be a separate "
            f"job definition, or the deploy would copy the job definition into itself."
        )
    return value


def describe_template(batch, name: str) -> Dict[str, Any]:
    """The template's latest ACTIVE revision. DescribeJobDefinitions returns every revision, paginated and in no
    guaranteed order, so take the highest revision number."""
    from botocore.exceptions import ClientError

    try:
        pages = batch.get_paginator("describe_job_definitions").paginate(jobDefinitionName=name, status="ACTIVE")
        revisions = [jd for page in pages for jd in page.get("jobDefinitions", [])]
    except ClientError as e:
        raise _client_error(e, "batch:DescribeJobDefinitions", f"job definition {name}") from e
    revisions = [jd for jd in revisions if jd.get("status", "ACTIVE") == "ACTIVE"]
    if not revisions:
        raise TemplateError(
            f"Template job definition {name!r} has no ACTIVE revision. Apply the Terraform that manages it."
        )
    template = max(revisions, key=lambda jd: jd["revision"])
    gtd.logger.info(f"Template: {template.get('jobDefinitionArn')} (revision {template.get('revision')})")
    return template


def build(ssm, batch, *, job_name: str, image: str) -> Dict[str, Any]:
    template = describe_template(batch, read_template_name(ssm, job_name))
    return render_from_template(template, job_name=job_name, image=image)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build an AWS Batch job definition from its Terraform-managed template job definition",
    )
    parser.add_argument("aws_region", help="AWS region")
    parser.add_argument("container_registry", help="Registry for the replaced image (empty for a public image)")
    parser.add_argument("image_name", help="Container image name")
    parser.add_argument("tag", help="Container image tag")
    parser.add_argument("job_name", help="Job definition name (the task_name input)")
    parser.add_argument("--deregister-previous", choices=["true", "false"], default="false",
                        help="Passed through to the register step (default: %(default)s)")
    parser.add_argument("--output", "-o", default="job-definition.json",
                        help="Output file path (default: %(default)s)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    gtd.logger = gtd.setup_logging(args.log_level)
    try:
        if not gjd.JOB_DEFINITION_NAME_PATTERN.match(args.job_name):
            raise TemplateError(
                f"task_name {args.job_name!r} is not a valid job definition name (up to 128 letters, digits, "
                f"'-' and '_')."
            )
        import boto3

        session = boto3.Session(region_name=args.aws_region)
        image = gtd.build_image_uri(args.container_registry, args.image_name, args.tag)
        job_def = build(session.client("ssm"), session.client("batch"), job_name=args.job_name, image=image)
    except TemplateError as e:
        gtd.logger.error(f"Template mode failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - credentials, network: report, don't traceback
        gtd.logger.error(f"Template mode failed: {type(e).__name__}: {e}")
        return 1

    Path(args.output).write_text(json.dumps(job_def, indent=2) + "\n")
    gtd.logger.info(f"Job definition written to {args.output}")
    gjd.emit_outputs({"deregister_previous": args.deregister_previous})
    print(json.dumps(job_def, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
