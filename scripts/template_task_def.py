#!/usr/bin/env python3
"""
Build the deploy task definition from a Terraform-managed template (task_definition_source: template).

terraform-aws-ecs-service (>= 3.2.0, `task_definition_template`) keeps a fully managed task definition in a
separate family and publishes two SSM parameters:

  /ecs/<cluster>/<service>/task-definition-template   the template family name (always, when enabled)
  /ecs/<cluster>/<service>/replica-count              the desired count (only when replica_count is set)

This script copies the template family's latest ACTIVE revision, sets `family` to the service's own family
(`<cluster>_<service>`), replaces the image of one container (`--container-name`, default `app`), removes the
read-only fields, and writes task-definition.json for the deploy step. Nothing else changes: no merging, no
defaults, no tags (the service's propagate_tags setting tags the tasks).

It emits `replica_count` like the YAML path: the parameter's value when it exists, empty otherwise, so the
deploy step leaves the service's desired count alone.
"""

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import generate_task_def as gtd

# Fields DescribeTaskDefinition returns that RegisterTaskDefinition rejects. The same list as the module's
# "Pipeline contract" (deleteRequestedAt appears only on revisions being deleted; stripping it is harmless).
READ_ONLY_FIELDS = (
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
    "deleteRequestedAt",
)


class TemplateError(Exception):
    """A template that can't be deployed, or AWS access that failed; the message says what to fix."""


def ssm_name(cluster: str, service: str, suffix: str) -> str:
    return f"/ecs/{cluster}/{service}/{suffix}"


def service_family(cluster: str, service: str) -> str:
    return f"{cluster}_{service}"


# --------------------------------------------------------------------------
# Pure core
# --------------------------------------------------------------------------

def render_from_template(template: Dict[str, Any], *, family: str, container_name: str, image: str) -> Dict[str, Any]:
    """Return a copy of `template` ready to register as `family`, with `container_name`'s image replaced."""
    task_def = copy.deepcopy(template)

    containers = task_def.get("containerDefinitions") or []
    matches = [c for c in containers if c.get("name") == container_name]
    if len(matches) != 1:
        names = ", ".join(repr(c.get("name")) for c in containers) or "none"
        found = "no container" if not matches else f"{len(matches)} containers"
        raise TemplateError(
            f"The template has {found} named {container_name!r} (its containers: {names}). Set container_name "
            f"to the container whose image this deploy replaces."
        )
    matches[0]["image"] = image

    for field in READ_ONLY_FIELDS:
        task_def.pop(field, None)
    task_def["family"] = family
    return task_def


# --------------------------------------------------------------------------
# AWS layer (clients are passed in; boto3 is imported only in main())
# --------------------------------------------------------------------------

def _client_error(error, action: str, resource: str) -> TemplateError:
    """Translate a botocore ClientError into an actionable message (the style of RoleResolver._client_error)."""
    code = error.response.get("Error", {}).get("Code", "Unknown")
    if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
        return TemplateError(f"Access denied: the deploy role needs {action} on {resource}.")
    if code in ("ThrottlingException", "TooManyUpdates", "RequestLimitExceeded"):
        return TemplateError(f"AWS throttled {action} ({code}) on {resource}. Retry the deploy.")
    if code in ("ExpiredTokenException", "ExpiredToken", "UnrecognizedClientException", "InvalidClientTokenId"):
        return TemplateError(f"AWS credentials are expired or invalid ({code}) while calling {action}.")
    return TemplateError(f"AWS error {code} from {action} on {resource}: {error}")


def _is_parameter_not_found(error) -> bool:
    return error.response.get("Error", {}).get("Code") == "ParameterNotFound"


def read_template_family(ssm, cluster: str, service: str) -> str:
    from botocore.exceptions import ClientError

    name = ssm_name(cluster, service, "task-definition-template")
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"].strip()
    except ClientError as e:
        if _is_parameter_not_found(e):
            raise TemplateError(
                f"SSM parameter {name} not found. Template mode needs `task_definition_template` enabled for this "
                f"service in terraform-aws-ecs-service (>= 3.2.0), which publishes the template family there."
            ) from e
        raise _client_error(e, "ssm:GetParameter", f"parameter {name}") from e
    if not value:
        raise TemplateError(f"SSM parameter {name} is empty; it should hold the template family name.")
    if value == service_family(cluster, service):
        raise TemplateError(
            f"SSM parameter {name} names the service's own family {value!r}; the template must be a separate "
            f"family, or the deploy would copy the service's family into itself."
        )
    return value


def read_replica_count(ssm, cluster: str, service: str) -> Optional[str]:
    """The desired count to set, or None to leave the service's count alone. Only a missing parameter means
    None: any other error fails the deploy, so a missing IAM grant can't silently skip a count."""
    from botocore.exceptions import ClientError

    name = ssm_name(cluster, service, "replica-count")
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as e:
        if _is_parameter_not_found(e):
            return None
        raise _client_error(e, "ssm:GetParameter", f"parameter {name}") from e


def describe_template(ecs, family: str) -> Dict[str, Any]:
    """The family's latest ACTIVE revision. No `include`: tags are not carried to the deploy family."""
    from botocore.exceptions import ClientError

    try:
        task_def = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("ClientException", "InvalidParameterException"):
            raise TemplateError(
                f"Template family {family!r} has no ACTIVE revision ({e}). Apply the Terraform that manages it."
            ) from e
        raise _client_error(e, "ecs:DescribeTaskDefinition", f"task definition family {family}") from e
    gtd.logger.info(f"Template: {task_def.get('taskDefinitionArn')} (revision {task_def.get('revision')})")
    return task_def


def build(ssm, ecs, *, cluster: str, service: str, container_name: str, image: str):
    """(task definition, replica_count or None)."""
    family = read_template_family(ssm, cluster, service)
    template = describe_template(ecs, family)
    task_def = render_from_template(template, family=service_family(cluster, service),
                                    container_name=container_name, image=image)
    return task_def, read_replica_count(ssm, cluster, service)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build an ECS task definition from the service's Terraform-managed template family",
    )
    parser.add_argument("cluster_name", help="ECS cluster name")
    parser.add_argument("aws_region", help="AWS region")
    parser.add_argument("container_registry", help="Registry for the replaced image (empty for a public image)")
    parser.add_argument("image_name", help="Container image name")
    parser.add_argument("tag", help="Container image tag")
    parser.add_argument("service_name", help="ECS service name")
    parser.add_argument("--container-name", default="app",
                        help="Container whose image is replaced (default: %(default)s)")
    parser.add_argument("--output", "-o", default="task-definition.json",
                        help="Output file path (default: %(default)s)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    gtd.logger = gtd.setup_logging(args.log_level)
    try:
        import boto3

        session = boto3.Session(region_name=args.aws_region)
        image = gtd.build_image_uri(args.container_registry, args.image_name, args.tag)
        task_def, replica_count = build(
            session.client("ssm"), session.client("ecs"),
            cluster=args.cluster_name, service=args.service_name,
            container_name=args.container_name, image=image,
        )
    except TemplateError as e:
        gtd.logger.error(f"Template mode failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - credentials, network: report, don't traceback
        gtd.logger.error(f"Template mode failed: {type(e).__name__}: {e}")
        return 1

    output_path = Path(args.output)
    with output_path.open("w") as handle:
        json.dump(task_def, handle, indent=2, default=str)
    gtd.logger.info(f"Task definition written to {output_path}")
    gtd.emit_replica_count(replica_count)
    print(json.dumps(task_def, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
