import logging
from typing import Any, Dict, Optional

from .common import ValidationError
from .config_loader import load_and_validate_config
from .containers import (
    build_app_container,
    build_fluent_bit_container,
    build_image_uri,
    build_init_containers,
    build_otel_container,
    parse_image_parts,
)
from .secrets_manager import SecretManager

logger = logging.getLogger(__name__)


def generate_task_definition(
    config_dict: Optional[Dict[str, Any]] = None,
    yaml_file_path: Optional[str] = None,
    cluster_name: Optional[str] = None,
    aws_region: Optional[str] = None,
    registry: Optional[str] = None,
    container_registry: Optional[str] = None,
    image_name: Optional[str] = None,
    tag: Optional[str] = None,
    service_name: Optional[str] = None,
    public_image: Optional[str] = None,
):
    """Generate an ECS task definition from a simplified YAML configuration."""
    # Load config if not provided
    if config_dict is None:
        if yaml_file_path is None:
            raise ValidationError("Either config_dict or yaml_file_path must be provided")
        config = load_and_validate_config(yaml_file_path)
    else:
        config = config_dict

    # Extract values from config
    # Use service name from action instead of YAML name
    app_name = service_name if service_name else config.get("name", "app")
    cpu = str(config.get("cpu", 256))
    memory = str(config.get("memory", 512))
    # OTEL Collector block (new format)
    otel_collector = config.get("otel_collector")
    if otel_collector is not None:
        otel_collector_image_name = otel_collector.get("image_name", "").strip()
        otel_collector_ssm = otel_collector.get("ssm_name", "adot-config-global.yaml").strip()
        otel_extra_config = otel_collector.get("extra_config", "").strip()
        otel_metrics_port = otel_collector.get("metrics_port", 8080)  # Default to 8080
        otel_metrics_path = otel_collector.get("metrics_path", "/metrics")  # Default to /metrics
        otel_is_custom_image = bool(otel_collector_image_name)
        if not otel_collector_image_name:
            otel_collector_image = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
        else:
            # Custom image name - ALWAYS use ECR registry (private image)
            logger.debug(
                f"registry='{registry}', otel_collector_image_name='{otel_collector_image_name}'"
            )
            # Registry is always available for OTEL/Fluent Bit
            otel_collector_image = f"{registry}/{otel_collector_image_name}"
            logger.debug(f"Using ECR registry - otel_collector_image='{otel_collector_image}'")
    else:
        otel_collector_image = None
        otel_is_custom_image = False
        otel_collector_ssm = ""
        otel_extra_config = ""
        otel_metrics_port = 8080
        otel_metrics_path = "/metrics"

    cpu_arch = config.get("cpu_arch", "X86_64")
    health_check = config.get("health_check", {})
    # Only build health check if config has values and command is non-empty
    if health_check and health_check.get("command"):
        health = {
            "command": ["CMD-SHELL", health_check["command"]],
            "interval": health_check.get("interval", 30),
            "timeout": health_check.get("timeout", 5),
            "retries": health_check.get("retries", 3),
            "startPeriod": health_check.get("start_period", 10),
        }
    else:
        health = None

    # Extract fluent_bit_collector config if present
    fluent_bit_collector = config.get("fluent_bit_collector", {})
    use_fluent_bit = bool(fluent_bit_collector and fluent_bit_collector.get("image_name", "").strip())
    # Handle fluent-bit image - ALWAYS ECR if image_name is specified
    if use_fluent_bit:
        fluent_bit_image_name = fluent_bit_collector.get("image_name", "").strip()
        # Registry is always available for OTEL/Fluent Bit
        fluent_bit_image = f"{registry}/{fluent_bit_image_name}"
    else:
        fluent_bit_image = ""

    # Get environment variables (changed from env_variables to envs)
    environment = []
    for env_var in config.get("envs", []):
        for key, value in env_var.items():
            environment.append(
                {
                    "name": key,
                    "value": str(value),  # Convert to string for ECS compatibility
                }
            )

    # Get secrets using the SecretManager
    secrets = SecretManager.build_secrets_from_config(config)

    # Check for secret_files configuration (multiple files now supported)
    secret_files = config.get("secret_files", [])
    has_secret_files = len(secret_files) > 0

    # Get configurable secrets files path (defaults to /etc/secrets)
    secrets_files_path = config.get("secrets_files_path", "/etc/secrets")

    # Create shared volume for secret files if needed
    volumes = []
    if has_secret_files:
        volumes.append(
            {
                "name": "shared-volume",
                "host": {},
            }
        )

    # Create volumes for writable directories (needed when readonlyRootFilesystem is true)
    writable_dirs = config.get("writable_dirs", [])
    for dir_path in writable_dirs:
        # Generate volume name from path: /tmp -> writable-tmp, /var/run -> writable-var-run
        vol_name = "writable-" + dir_path.strip("/").replace("/", "-")
        volumes.append(
            {
                "name": vol_name,
                "host": {},
            }
        )

    # Sanitize image_name and tag for ECR URI
    image_name_clean, tag_clean = parse_image_parts(image_name or "", tag or "")
    image_uri = build_image_uri(container_registry, image_name_clean, tag_clean)

    logger.info(f"Setting container image to: {image_uri}")

    # Get launch type and network mode (defaults for backwards compatibility)
    launch_type = config.get("launch_type", "FARGATE").upper()
    network_mode = config.get("network_mode", "awsvpc").lower()

    logger.info(f"Launch type: {launch_type}, Network mode: {network_mode}")

    # Create the container definitions list
    container_definitions = []

    # Create init containers for secret files if needed
    init_containers = build_init_containers(
        config, secret_files, cluster_name, app_name, aws_region, secrets_files_path
    )
    container_definitions.extend(init_containers)

    # Add the main application container
    app_container = build_app_container(
        config,
        image_uri,
        environment,
        secrets,
        health,
        cluster_name,
        app_name,
        aws_region,
        use_fluent_bit,
        has_secret_files,
        secrets_files_path,
        network_mode,
        launch_type,
    )
    container_definitions.append(app_container)

    # Add fluent-bit sidecar container if enabled
    if use_fluent_bit:
        fluent_bit_container = build_fluent_bit_container(
            config, fluent_bit_image, app_name, cluster_name, aws_region
        )
        container_definitions.append(fluent_bit_container)

    # Add the OpenTelemetry collector container if enabled (new format)
    if otel_collector_image is not None:
        otel_container = build_otel_container(
            config,
            otel_collector_image,
            otel_is_custom_image,
            otel_collector_ssm,
            otel_extra_config,
            otel_metrics_port,
            otel_metrics_path,
            app_name,
            cluster_name,
            aws_region,
        )
        container_definitions.append(otel_container)

    # Apply readonlyRootFilesystem to ALL containers if specified
    readonly_root_filesystem = config.get("readonly_root_filesystem")
    if readonly_root_filesystem is not None:
        for container in container_definitions:
            container["readonlyRootFilesystem"] = bool(readonly_root_filesystem)

    # Add writable_dirs mountPoints to ALL containers if specified
    if writable_dirs:
        for container in container_definitions:
            if "mountPoints" not in container:
                container["mountPoints"] = []
            for dir_path in writable_dirs:
                vol_name = "writable-" + dir_path.strip("/").replace("/", "-")
                container["mountPoints"].append(
                    {
                        "sourceVolume": vol_name,
                        "containerPath": dir_path,
                    }
                )

    # Create the complete task definition
    task_definition = {
        "containerDefinitions": container_definitions,
        "cpu": cpu,
        "memory": memory,
        "family": f"{cluster_name}_{app_name}",
        "taskRoleArn": config.get("role_arn", ""),
        "executionRoleArn": config.get("role_arn", ""),
        "networkMode": network_mode,
        "requiresCompatibilities": [launch_type],
    }

    # Add runtimePlatform only for Fargate (required for Fargate, not needed for EC2)
    if launch_type == "FARGATE":
        task_definition["runtimePlatform"] = {
            "cpuArchitecture": cpu_arch,
            "operatingSystemFamily": "LINUX",
        }

    # Add ephemeral storage if specified
    ephemeral_storage = config.get("ephemeral_storage")
    if ephemeral_storage is not None:
        task_definition["ephemeralStorage"] = {
            "sizeInGiB": int(ephemeral_storage)
        }
        logger.info(f"Set ephemeral storage size to {ephemeral_storage} GiB")

    # Add volumes if needed
    if volumes:
        task_definition["volumes"] = volumes

    return task_definition
