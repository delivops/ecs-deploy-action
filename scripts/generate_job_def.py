#!/usr/bin/env python3
"""Generate an AWS Batch job definition from the same task_config_yaml format.

Used by deployment_type=batch_job_definition. Everything the ECS path already
does that means the same thing for Batch is reused from generate_task_def.py -
YAML loading, services_overrides, envs_from_files, envs, the three secrets
formats, image URI and role resolution - so the two paths cannot drift apart.

Output is the request body of `aws batch register-job-definition
--cli-input-json`: a "container" job with containerProperties.
"""
import argparse
import json
import logging
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

import generate_task_def as gtd  # noqa: E402
from generate_task_def import (  # noqa: E402
    RoleResolutionError, RoleResolver, SecretManager, ValidationError,
    build_environment, build_image_uri, normalize_role_value, validate_role_arn,
)

logger = logging.getLogger(__name__)

PLATFORMS = ('EC2', 'FARGATE')

# Job definition names: up to 128 letters, numbers, hyphens and underscores.
JOB_DEFINITION_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,128}$')

# Batch parameter names are substituted as Ref::<name>.
PARAMETER_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

# Fargate vCPU -> valid memory (MiB), from the Batch ResourceRequirement docs.
FARGATE_MEMORY_FOR_VCPU = {
    Decimal('0.25'): [512, 1024, 2048],
    Decimal('0.5'): [1024, 2048, 3072, 4096],
    Decimal('1'): list(range(2048, 8193, 1024)),
    Decimal('2'): list(range(4096, 16385, 1024)),
    Decimal('4'): list(range(8192, 30721, 1024)),
    Decimal('8'): list(range(16384, 61441, 4096)),
    Decimal('16'): list(range(32768, 122881, 8192)),
    Decimal('32'): [61440, 122880, 249856],
}

DEFAULT_VCPUS = 1
DEFAULT_MEMORY = 2048

# Every top-level YAML key the Batch renderer reads. Anything else in the YAML
# (ports, health_check, sidecars, replica_count, ...) has no Batch equivalent
# and is ignored with a warning, so a YAML shared with a service still works.
BATCH_KEYS = frozenset({
    'name', 'cpu', 'memory', 'cpu_arch', 'ephemeral_storage',
    'command', 'envs', 'secrets', 'secrets_envs',
    'role_arn', 'task_role_arn', 'execution_role_arn',
    'platform', 'timeout_seconds', 'retry_attempts', 'parameters',
    'propagate_tags', 'deregister_previous',
})

# Keys that would silently change what the container runs if ignored, so they
# fail instead of warning.
REJECTED_KEYS = {
    'entrypoint': (
        "AWS Batch job definitions have no entryPoint field, so 'entrypoint' cannot "
        "be honoured. Remove it and put the full command in 'command' (or bake the "
        "ENTRYPOINT into the image)."
    ),
    'secret_files': (
        "'secret_files' needs an init container and a shared volume, which a Batch "
        "container job does not have. Inject the values with 'secrets' / "
        "'secrets_envs' instead."
    ),
}


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _format_vcpus(value: Decimal) -> str:
    """'0.25', '1', '16' - Batch takes the vCPU count as a string."""
    return format(value.normalize(), 'f')


def _parameter_value(value: Any) -> str:
    """Batch parameter defaults are strings; keep YAML booleans lowercase."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_vcpus(raw: Any, platform: str) -> Decimal:
    """`cpu` in vCPUs for Batch. Fractional values are Fargate-only."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValidationError(f"cpu must be a number of vCPUs, got {raw!r}")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ValidationError(f"cpu must be a number of vCPUs, got {raw!r}")
    if not value.is_finite() or value <= 0:
        raise ValidationError(f"cpu must be a positive number of vCPUs, got {raw!r}")

    # ECS task configs express cpu in CPU units (1024 = 1 vCPU). Catch a shared
    # YAML's `cpu: 256` before it asks Batch for 256 vCPUs.
    if value >= 256 and value % 256 == 0:
        raise ValidationError(
            f"cpu is {raw} - for batch_job_definition cpu is a number of vCPUs, not "
            f"ECS CPU units. Did you mean {_format_vcpus(value / 1024)}? Override it "
            f"for this job under services_overrides.<task_name>.cpu."
        )

    if platform == 'FARGATE':
        if value not in FARGATE_MEMORY_FOR_VCPU:
            raise ValidationError(
                f"Invalid cpu {raw} for platform FARGATE. Must be one of "
                f"{[_format_vcpus(v) for v in FARGATE_MEMORY_FOR_VCPU]} vCPUs."
            )
    elif value != value.to_integral_value():
        raise ValidationError(
            f"Invalid cpu {raw} for platform EC2: fractional vCPUs are only "
            f"supported on FARGATE. Use a whole number of vCPUs."
        )
    return value


def parse_memory(raw: Any, vcpus: Decimal, platform: str) -> int:
    if not _is_positive_int(raw):
        raise ValidationError(f"memory must be a positive integer (MiB), got {raw!r}")
    if platform == 'FARGATE':
        valid = FARGATE_MEMORY_FOR_VCPU[vcpus]
        if raw not in valid:
            raise ValidationError(
                f"Invalid memory {raw} for {_format_vcpus(vcpus)} vCPU on FARGATE. "
                f"Valid values are: {valid}"
            )
    return raw


def validate_batch_config(config: Dict[str, Any]) -> None:
    """Validate a loaded task config for batch_job_definition.

    Replaces generate_task_def.validate_config for this type: cpu means vCPUs
    here, and launch_type / network_mode / sidecars are not Batch concepts.
    """
    for key, message in REJECTED_KEYS.items():
        if config.get(key):
            raise ValidationError(message)

    platform = config.get('platform', 'EC2')
    if not isinstance(platform, str) or platform.upper() not in PLATFORMS:
        raise ValidationError(f"Invalid platform: {platform!r}. Must be one of {list(PLATFORMS)}")
    platform = platform.upper()

    vcpus = parse_vcpus(config.get('cpu', DEFAULT_VCPUS), platform)
    parse_memory(config.get('memory', DEFAULT_MEMORY), vcpus, platform)

    for key in ('propagate_tags', 'deregister_previous'):
        value = config.get(key)
        if value is not None and not _is_bool(value):
            raise ValidationError(f"{key} must be true or false, got {value!r}")

    timeout = config.get('timeout_seconds')
    if timeout is not None and (not _is_positive_int(timeout) or timeout < 60):
        raise ValidationError(
            f"timeout_seconds must be an integer of at least 60 (the Batch minimum), "
            f"got {timeout!r}"
        )

    attempts = config.get('retry_attempts')
    if attempts is not None and (not _is_positive_int(attempts) or attempts > 10):
        raise ValidationError(f"retry_attempts must be an integer from 1 to 10, got {attempts!r}")

    parameters = config.get('parameters')
    if parameters is not None:
        if not isinstance(parameters, dict):
            raise ValidationError(
                f"parameters must be a mapping of name to default value, got "
                f"{type(parameters).__name__}"
            )
        for name, default in parameters.items():
            if not isinstance(name, str) or not PARAMETER_NAME_PATTERN.match(name):
                raise ValidationError(
                    f"Invalid parameter name {name!r}: use letters, digits, '-' and '_'"
                )
            if isinstance(default, (dict, list)) or default is None:
                raise ValidationError(
                    f"parameters.{name} must be a scalar default value, got {default!r}"
                )

    ephemeral = config.get('ephemeral_storage')
    if ephemeral is not None:
        if platform != 'FARGATE':
            raise ValidationError(
                "ephemeral_storage is only supported for platform FARGATE in AWS Batch"
            )
        if not _is_positive_int(ephemeral) or not 21 <= ephemeral <= 200:
            raise ValidationError(
                f"ephemeral_storage must be an integer from 21 to 200 (GiB), got {ephemeral!r}"
            )

    cpu_arch = config.get('cpu_arch', 'X86_64')
    if cpu_arch not in ('X86_64', 'ARM64'):
        raise ValidationError(f"Invalid cpu_arch: {cpu_arch!r}. Must be X86_64 or ARM64")

    command = config.get('command')
    if command is not None and not isinstance(command, list):
        raise ValidationError(f"command must be a list, got {type(command).__name__}")

    for key in gtd.ROLE_ARN_KEYS:
        value = normalize_role_value(config, key)
        if value is not None:
            validate_role_arn(value, f"YAML key '{key}'")


def warn_ignored_keys(config: Dict[str, Any]) -> List[str]:
    """Warn about every key that has no Batch equivalent. Returns them sorted."""
    ignored = sorted(k for k in config if k not in BATCH_KEYS and k not in REJECTED_KEYS)
    for key in ignored:
        message = f"'{key}' does not apply to batch_job_definition and is ignored"
        logger.warning(message)
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            # Surface it as a run annotation too. stderr: stdout carries the JSON.
            print(f"::warning::{message}", file=sys.stderr)
    return ignored


def resolve_job_roles(config: Dict[str, Any], cluster_name: Optional[str],
                      job_name: str, aws_region: str, platform: str,
                      has_secrets: bool) -> Dict[str, str]:
    """Resolve jobRoleArn / executionRoleArn.

    Same order as the ECS types - per-slot YAML key, then role_arn - and, when
    an ecs_cluster is given, the same SSM parameters /ecs/<cluster>/<name>/...
    (both slots mandatory, exactly as RoleResolver enforces). Without a cluster
    there is no SSM fallback: the job role is optional, and the execution role
    is required only where Batch needs it (Fargate, or any secrets).
    """
    if cluster_name:
        resolved = RoleResolver(cluster_name, job_name, aws_region).resolve(config)
    else:
        shared = normalize_role_value(config, 'role_arn', RoleResolutionError)
        resolved = {}
        for yaml_key, td_key, _ in RoleResolver.SLOTS:
            value = normalize_role_value(config, yaml_key, RoleResolutionError) or shared
            if value:
                validate_role_arn(value, f"YAML key '{yaml_key}' / 'role_arn'", RoleResolutionError)
                resolved[td_key] = value

        if 'executionRoleArn' not in resolved and (platform == 'FARGATE' or has_secrets):
            reason = "platform FARGATE" if platform == 'FARGATE' else "secrets"
            raise RoleResolutionError(
                f"Could not determine executionRoleArn for job definition '{job_name}': "
                f"{reason} requires one. Set 'execution_role_arn' (or 'role_arn') in "
                f"the task config YAML, or pass ecs_cluster to read it from SSM "
                f"/ecs/<cluster>/{job_name}/execution-role."
            )

    roles = {}
    if 'taskRoleArn' in resolved:
        roles['jobRoleArn'] = resolved['taskRoleArn']
    if 'executionRoleArn' in resolved:
        roles['executionRoleArn'] = resolved['executionRoleArn']
    return roles


def generate_job_definition(config: Dict[str, Any], job_name: str,
                            cluster_name: Optional[str], aws_region: str,
                            container_registry: Optional[str], image_name: str,
                            tag: str) -> Dict[str, Any]:
    """Render a register-job-definition request body from a loaded config."""
    if not job_name or not JOB_DEFINITION_NAME_PATTERN.match(job_name):
        raise ValidationError(
            f"Invalid job definition name {job_name!r} (task_name): up to 128 letters, "
            f"digits, '-' and '_'."
        )

    validate_batch_config(config)
    warn_ignored_keys(config)

    platform = config.get('platform', 'EC2').upper()
    vcpus = parse_vcpus(config.get('cpu', DEFAULT_VCPUS), platform)
    memory = parse_memory(config.get('memory', DEFAULT_MEMORY), vcpus, platform)

    secrets = SecretManager.build_secrets_from_config(config)

    container: Dict[str, Any] = {
        "image": build_image_uri(container_registry, image_name, tag),
        "resourceRequirements": [
            {"type": "VCPU", "value": _format_vcpus(vcpus)},
            {"type": "MEMORY", "value": str(memory)},
        ],
    }
    # Omitted rather than empty: an absent command means "use the image's CMD".
    command = config.get('command') or []
    if command:
        container["command"] = [str(part) for part in command]
    environment = build_environment(config)
    if environment:
        container["environment"] = environment
    if secrets:
        container["secrets"] = secrets

    container.update(resolve_job_roles(
        config, cluster_name, job_name, aws_region, platform, bool(secrets)
    ))

    # Same log group convention as the ECS types. Without a cluster there is no
    # convention to follow, so Batch's default (/aws/batch/job) applies.
    if cluster_name:
        container["logConfiguration"] = {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{cluster_name}/{job_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "batch",
            },
        }
    else:
        logger.info("No ecs_cluster given: logs go to Batch's default group /aws/batch/job")

    if platform == 'FARGATE':
        container["runtimePlatform"] = {
            "operatingSystemFamily": "LINUX",
            "cpuArchitecture": config.get('cpu_arch', 'X86_64'),
        }
        ephemeral = config.get('ephemeral_storage')
        if ephemeral is not None:
            container["ephemeralStorage"] = {"sizeInGiB": ephemeral}

    job_definition: Dict[str, Any] = {
        "jobDefinitionName": job_name,
        "type": "container",
        "platformCapabilities": [platform],
        "containerProperties": container,
        "retryStrategy": {"attempts": config.get('retry_attempts', 1)},
        "propagateTags": config.get('propagate_tags', True),
    }

    parameters = config.get('parameters')
    if parameters:
        job_definition["parameters"] = {k: _parameter_value(v) for k, v in parameters.items()}

    timeout = config.get('timeout_seconds')
    if timeout is not None:
        job_definition["timeout"] = {"attemptDurationSeconds": timeout}

    return job_definition


def load_batch_config(yaml_file: str, job_name: str) -> Dict[str, Any]:
    """Load the YAML with services_overrides and envs_from_files, Batch validation."""
    return gtd.load_and_validate_config(yaml_file, job_name, validator=validate_batch_config)


def emit_outputs(outputs: Dict[str, str]) -> None:
    github_output = os.environ.get('GITHUB_OUTPUT')
    if not github_output:
        return
    with open(github_output, 'a', encoding='utf-8') as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Generate an AWS Batch job definition from a task config YAML')
    parser.add_argument('yaml_file', help='Path to the task config YAML')
    parser.add_argument('job_name', help='Job definition name (the task_name input)')
    parser.add_argument('aws_region', help='AWS region for log configuration and SSM')
    parser.add_argument('container_registry', help='Registry for the image (empty for none)')
    parser.add_argument('image_name', help='Container image name')
    parser.add_argument('tag', help='Container image tag')
    parser.add_argument('--cluster', default='',
                        help='ECS cluster: enables the SSM role fallback and the /ecs/<cluster>/<name> log group')
    parser.add_argument('--output', '-o', default='job-definition.json')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if not Path(args.yaml_file).exists():
            raise ValidationError(f"YAML file not found: {args.yaml_file}")
        config = load_batch_config(args.yaml_file, args.job_name)
        job_definition = generate_job_definition(
            config, args.job_name, args.cluster or None, args.aws_region,
            args.container_registry, args.image_name, args.tag,
        )
    except RoleResolutionError as e:
        logger.error(f"Role resolution failed:\n{e}")
        return 1
    except ValidationError as e:
        logger.error(f"Configuration validation failed: {e}")
        return 1

    Path(args.output).write_text(json.dumps(job_definition, indent=2) + "\n")
    logger.info(f"Job definition written to {args.output}")
    emit_outputs({
        "deregister_previous": "true" if config.get('deregister_previous') else "false",
    })
    print(json.dumps(job_definition, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
