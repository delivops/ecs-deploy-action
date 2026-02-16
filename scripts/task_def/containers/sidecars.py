def build_fluent_bit_container(config, fluent_bit_image, app_name, cluster_name, aws_region):
    """Build Fluent Bit sidecar container"""
    fluent_bit_collector = config.get("fluent_bit_collector", {})
    config_name = fluent_bit_collector.get("extra_config", "extra.conf")
    ecs_log_metadata = fluent_bit_collector.get("ecs_log_metadata", "true")
    # Allow custom service_name, default to app_name if not specified
    fluent_bit_service_name = fluent_bit_collector.get("service_name", app_name)
    extra_config = f"extra/{config_name}"

    fluent_bit_container = {
        "name": "fluent-bit",
        "image": fluent_bit_image,  # Always ECR-style
        "essential": True,  # Critical sidecar - if it fails, task should fail
        "environment": [
            {"name": "SERVICE_NAME", "value": fluent_bit_service_name},
            {"name": "ENV", "value": cluster_name},
        ],
        "healthCheck": {
            "command": [
                "CMD-SHELL",
                "curl -f http://127.0.0.1:2020/api/v1/health || exit 1",
            ],
            "interval": 10,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 5,
        },
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "fluentbit",
            },
        },
        "firelensConfiguration": {
            "type": "fluentbit",
            "options": {
                "config-file-type": "file",
                "config-file-value": extra_config,
                "enable-ecs-log-metadata": ecs_log_metadata,
            },
        },
    }

    return fluent_bit_container


def build_otel_container(
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
):
    """Build OpenTelemetry collector container"""
    # Build environment variables for OTEL container
    otel_environment = []

    # Always add METRICS_PATH (default: /metrics)
    otel_environment.append({"name": "METRICS_PATH", "value": otel_metrics_path})

    # Always add METRICS_PORT (default: 8080)
    otel_environment.append({"name": "METRICS_PORT", "value": str(otel_metrics_port)})

    # Add SERVICE_NAME if using custom image (not default AWS image)
    if otel_is_custom_image:
        otel_environment.append({"name": "SERVICE_NAME", "value": app_name})

    # Build command based on image type
    if otel_is_custom_image and otel_extra_config:
        # Custom image with extra config file
        otel_command = ["--config", f"/conf/{otel_extra_config}"]
    elif otel_is_custom_image:
        # Custom image without extra config (use default config path)
        otel_command = ["--config", "/conf/config.yaml"]
    else:
        # Default AWS image - use SSM config
        otel_command = ["--config", "env:SSM_CONFIG"]

    otel_container = {
        "name": "otel-collector",
        "image": otel_collector_image,  # Use as-is from YAML or default
        "portMappings": [
            {
                "name": "otel-collector-4317-tcp",
                "containerPort": 4317,
                "hostPort": 4317,
                "protocol": "tcp",
                "appProtocol": "grpc",
            },
            {
                "name": "otel-collector-4318-tcp",
                "containerPort": 4318,
                "hostPort": 4318,
                "protocol": "tcp",
            },
        ],
        "essential": True,  # Critical sidecar - if it fails, task should fail
        "command": otel_command,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "otel-collector",
            },
        },
    }

    # Add environment variables if any
    if otel_environment:
        otel_container["environment"] = otel_environment

    # Add secrets only for default AWS image
    if not otel_is_custom_image:
        otel_container["secrets"] = [{"name": "SSM_CONFIG", "valueFrom": otel_collector_ssm}]

    return otel_container
