from .base import ContainerBuilder
from .linux_parameters import build_linux_parameters


def build_app_container(
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
    secrets_files_path="/etc/secrets",
    network_mode="awsvpc",
    launch_type="FARGATE",
):
    """Build the main application container definition for the ECS task."""
    command = config.get("command", [])
    entrypoint = config.get("entrypoint", [])
    stop_timeout = config.get("stop_timeout")

    container_builder = ContainerBuilder(cluster_name, app_name, aws_region)

    app_container = {
        "name": "app",
        "image": image_uri,
        "essential": True,
        "environment": environment,
        "command": command,
        "entryPoint": entrypoint,
        "secrets": secrets,
    }

    # Add stopTimeout if specified
    if stop_timeout is not None:
        app_container["stopTimeout"] = int(stop_timeout)

    # Set logConfiguration for app container
    if use_fluent_bit:
        app_container["logConfiguration"] = {
            "logDriver": "awsfirelens",
            "options": {},
        }
    else:
        app_container["logConfiguration"] = container_builder.build_log_configuration(
            stream_prefix="default"
        )

    # Only include healthCheck if it was properly built
    if health:
        app_container["healthCheck"] = health

    # Handle port configurations
    main_port = config.get("port")
    additional_ports = config.get("additional_ports", [])
    app_protocol = config.get("app_protocol", "http")

    port_mappings = container_builder.build_port_mappings(
        main_port, additional_ports, app_protocol, network_mode
    )
    if port_mappings:
        app_container["portMappings"] = port_mappings

    # Add linuxParameters if configured
    linux_parameters = build_linux_parameters(config, launch_type)
    if linux_parameters:
        app_container["linuxParameters"] = linux_parameters

    # Add mount points if using shared volume
    if has_secret_files:
        app_container["mountPoints"] = [
            {
                "sourceVolume": "shared-volume",
                "containerPath": secrets_files_path,
            }
        ]
        # Add dependency on init containers
        app_depends_on = [
            {
                "containerName": "init-container-for-secret-files",
                "condition": "SUCCESS",
            }
        ]
    else:
        app_depends_on = []

    # If fluent-bit is enabled, add dependsOn for fluent-bit
    if use_fluent_bit:
        app_depends_on.append(
            {
                "containerName": "fluent-bit",
                "condition": "START",
            }
        )
    if app_depends_on:
        app_container["dependsOn"] = app_depends_on

    return app_container
