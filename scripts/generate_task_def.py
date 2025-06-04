#!/usr/bin/env python3
import yaml
import json
import argparse
import sys
import os

def generate_task_definition(yaml_file_path, cluster_name, aws_region, registry=None, image_name=None, tag=None, public_image=None):
    """
    Generate an ECS task definition from a simplified YAML configuration
    
    Args:
        yaml_file_path (str): Path to the YAML configuration file
        aws_region (str): AWS region to use for log configuration
        registry (str): ECR registry URL
        image_name (str): Image name
        tag (str): Image tag
    
    Returns:
        dict: The generated task definition
    """ 
    # Read the YAML file
    with open(yaml_file_path, 'r') as file:
        config = yaml.safe_load(file)
    
    # Extract values from config
    app_name = config.get('name', 'app')
    cpu = str(config.get('cpu', 256))
    memory = str(config.get('memory', 512))
    # OTEL Collector block (new format)
    otel_collector = config.get('otel_collector')
    if otel_collector is not None:
        otel_collector_image = otel_collector.get('image_name', '').strip()
        if not otel_collector_image:
            otel_collector_image = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
    else:
        otel_collector_image = None
    cpu_arch = config.get('cpu_arch', 'X86_64')
    command = config.get('command', [])
    entrypoint = config.get('entrypoint', [])
    health_check = config.get('health_check', {})
    # Only build health check if config has values and command is non-empty
    if health_check and health_check.get('command'):
        health = {
            "command": ["CMD-SHELL", health_check["command"]],
            "interval": health_check.get('interval', 30),
            "timeout": health_check.get('timeout', 5),
            "retries": health_check.get('retries', 3),
            "startPeriod": health_check.get('start_period', 10)
        }
    else:
        health = None
    
    # Extract replica_count for later use in the GitHub Action
    replica_count = config.get('replica_count', '')

    # Extract fluent_bit_collector config if present
    fluent_bit_collector = config.get('fluent_bit_collector', {})
    use_fluent_bit = bool(fluent_bit_collector and fluent_bit_collector.get('image_name', '').strip())
    config_name = fluent_bit_collector.get('extra_config', "extra.conf")
    ecs_log_metadata = fluent_bit_collector.get('ecs_log_metadata', 'true')
    extra_config = f"extra/{config_name}"
    # Use ECR-style image for fluent-bit sidecar, using fluent_bit_collector.image_name (without tag)
    if use_fluent_bit:
        fluent_bit_image_name = fluent_bit_collector.get('image_name', '').strip()
        fluent_bit_image = f"{registry}/{fluent_bit_image_name}"
    else:
        fluent_bit_image = ''
    
    # Get environment variables (changed from env_variables to envs)
    environment = []
    for env_var in config.get('envs', []):
        for key, value in env_var.items():
            environment.append({
                "name": key,
                "value": value
            })
    
    # Get secrets directly from YAML without AWS access
    secrets = []
    secret_list = config.get('secrets', [])
    for secret_dict in secret_list:
        for key, base_arn in secret_dict.items():
            secrets.append({
                "name": key,
                "valueFrom": f"{base_arn}:{key}::"
            })
    
    # Check for secret_files configuration (multiple files now supported)
    secret_files = config.get('secret_files', [])
    has_secret_files = len(secret_files) > 0
    
    # Create shared volume for secret files if needed
    volumes = []
    if has_secret_files:
        volumes.append({
            "name": "shared-volume",
            "host": {}
        })

    # Sanitize image_name and tag for ECR URI
    def parse_image_parts(image_name, tag):
        # Remove registry if mistakenly included in image_name
        if '/' in image_name and '.' in image_name.split('/')[0]:
            # Remove registry part
            image_name = '/'.join(image_name.split('/')[1:])
        # Remove tag from image_name if present
        if ':' in image_name:
            image_name, image_tag = image_name.split(':', 1)
            if not tag:
                tag = image_tag
        return image_name, tag

    image_name_clean, tag_clean = parse_image_parts(image_name, tag)

    if registry:
        image_uri = f"{registry}/{image_name_clean}:{tag_clean}"
    else:
        image_uri = f"{image_name_clean}:{tag_clean}"
    

    # Create app container definition
    app_container = {
        "name": "app",
        "image": image_uri,
        "essential": True,
        "environment": environment,
        "command": command,
        "entryPoint": entrypoint,
        "secrets": secrets
    }
    # Set logConfiguration for app container
    if use_fluent_bit:
        app_container["logConfiguration"] = {
            "logDriver": "awsfirelens",
            "options": {}
        }
    else:
        app_container["logConfiguration"] = {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "/default"
            }
        }
    
        # Only include healthCheck if it was properly built
    if health:
        app_container["healthCheck"] = health

    # Handle port configurations with new naming
    main_port = config.get('port')
    additional_ports = config.get('additional_ports', [])
    
    port_mappings = []
    
    # Add main port if specified with default name
    if main_port:
        port_mapping = {
            "name": "default",
            "containerPort": main_port,
            "hostPort": main_port,
            "protocol": "tcp",
            "appProtocol": "http"
        }
        port_mappings.append(port_mapping)
    
    # Add additional ports with their specified names
    for port_info in additional_ports:
        if isinstance(port_info, dict):
            # Each item is expected to be a dict with one key-value pair
            for name, port in port_info.items():
                port_mapping = {
                    "name": name,
                    "containerPort": port,
                    "hostPort": port,
                    "protocol": "tcp",
                    "appProtocol": "http"
                }
                port_mappings.append(port_mapping)
    
    if port_mappings:
        app_container["portMappings"] = port_mappings
    
    # Add mount points if using shared volume
    if has_secret_files:
        app_container["mountPoints"] = [
            {
                "sourceVolume": "shared-volume",
                "containerPath": "/etc/secrets"
            }
        ]
        # Add dependency on init containers
        app_depends_on = [
            {
                "containerName": "init-container-for-secret-files",
                "condition": "SUCCESS"
            }
        ]
    else:
        app_depends_on = []

    # If fluent-bit is enabled, add dependsOn for fluent-bit
    if use_fluent_bit:
        app_depends_on.append({
            "containerName": "fluent-bit",
            "condition": "START"
        })
    if app_depends_on:
        app_container["dependsOn"] = app_depends_on
    
    print(f"Setting container image to: {image_uri}")
    
    # Create the container definitions list
    container_definitions = []
    
    # Create init container for secret files if needed
    if has_secret_files:
        # Join secret names with commas for the environment variable
        secret_files_env = ",".join(secret_files)
        
        init_container = {
            "name": "init-container-for-secret-files",
            "image": "amazon/aws-cli",
            "essential": False,
            "entryPoint": ["/bin/sh"],
            "command": [
                "-c",
                "for secret in ${SECRET_FILES//,/ }; do "
                "echo \"Fetching $secret...\"; "
                "aws secretsmanager get-secret-value --secret-id $secret --region $AWS_REGION --query SecretString --output text > /etc/secrets/$secret; "
                "if [ $? -eq 0 ] && [ -s /etc/secrets/$secret ]; then "
                "echo \"✅ Successfully saved $secret to /etc/secrets/$secret\"; "
                "else echo \"❌ Failed to save $secret\" >&2; exit 1; "
                "fi; "
                "done"
            ],
            "environment": [
                {
                    "name": "SECRET_FILES",
                    "value": secret_files_env
                },
                {
                    "name": "AWS_REGION",
                    "value": aws_region
                }
            ],
            "mountPoints": [
                {
                    "sourceVolume": "shared-volume",
                    "containerPath": "/etc/secrets"
                }
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                    "awslogs-region": aws_region,
                    "awslogs-stream-prefix": "ssm-file-downloader"                
                    }
            }
        }
        container_definitions.append(init_container)

    # Add the main application container
    container_definitions.append(app_container)

    # Add fluent-bit sidecar container if enabled
    if use_fluent_bit:
        fluent_bit_container = {
            "name": "fluent-bit",
            "image": fluent_bit_image,  # Always ECR-style
            "essential": False,
            "environment": [
                {"name": "SERVICE_NAME", "value": app_name}
            ],
            "healthCheck": {
                "command": [
                    "CMD-SHELL",
                    "curl -f http://127.0.0.1:2020/api/v1/health || exit 1"
                ],
                "interval": 10,
                "timeout": 5,
                "retries": 3,
                "startPeriod": 5
            },
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                    "awslogs-region": aws_region,
                    "awslogs-stream-prefix": "fluentbit"
                }
            },
            "firelensConfiguration": {
                "type": "fluentbit",
                "options": {
                    "config-file-type": "file",
                    "config-file-value": extra_config,
                    "enable-ecs-log-metadata": ecs_log_metadata
                }
            }
        }
        container_definitions.append(fluent_bit_container)
    
    # Add the OpenTelemetry collector container if enabled (new format)
    if otel_collector_image is not None:
        otel_container = {
            "name": "otel-collector",
            "image": otel_collector_image,  # Use as-is from YAML or default
            "portMappings": [
                {
                    "name": "otel-collector-4317-tcp",
                    "containerPort": 4317,
                    "hostPort": 4317,
                    "protocol": "tcp",
                    "appProtocol": "grpc"
                },
                {
                    "name": "otel-collector-4318-tcp",
                    "containerPort": 4318,
                    "hostPort": 4318,
                    "protocol": "tcp"
                }
            ],
            "essential": False,
            # Remove SSM config logic if not needed
            "command": [
                "--config",
                "env:SSM_CONFIG"
            ],
            # Optionally remove secrets if not needed, or keep if required
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                    "awslogs-region": aws_region,
                    "awslogs-stream-prefix": "otel-collector"
                }
            }
        }
        container_definitions.append(otel_container)
    
    # Create the complete task definition
    task_definition = {
        "containerDefinitions": container_definitions,
        "cpu": cpu,
        "memory": memory,
        "runtimePlatform": {
            "cpuArchitecture": cpu_arch,
            "operatingSystemFamily": "LINUX"
        },
        "family": f"{cluster_name}_{app_name}",
        "taskRoleArn": config.get('role_arn', ''),
        "executionRoleArn": config.get('role_arn', ''),
        "networkMode": "awsvpc",
        "requiresCompatibilities": [
            "FARGATE"
        ]
    }
    
    # Add volumes if needed
    if has_secret_files and volumes:
        task_definition["volumes"] = volumes
    
    # Output the replica count to output
    print(f"::set-output name=replica_count::{replica_count}")

    return task_definition

def parse_args():
    """Parse and validate command line arguments"""
    parser = argparse.ArgumentParser(description='Generate ECS task definition from YAML configuration')
    
    parser.add_argument('yaml_file', help='Path to the YAML configuration file')
    parser.add_argument('cluster_name', help='The cluster name')
    parser.add_argument('aws_region', help='AWS region for log configuration')
    parser.add_argument('registry', help='ECR registry URL')
    parser.add_argument('image_name', help='Container image name')
    parser.add_argument('tag', help='Container image tag')
    parser.add_argument('--output', default='task-definition.json', help='Output file path (default: task-definition.json)')
    
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    
    try:
        task_definition = generate_task_definition(
            args.yaml_file,
            args.cluster_name,
            args.aws_region,
            args.registry,
            args.image_name,
            args.tag
        )
        
        # Write to the specified output file
        with open(args.output, 'w') as file:
            json.dump(task_definition, file, indent=2)

        print("\n----- Task Definition -----")
        print(json.dumps(task_definition, indent=2))
        print("---------------------------\n")
            
        print(f"Task definition successfully written to {args.output}")
        
    except Exception as e:
        print(f"Error generating task definition: {e}", file=sys.stderr)
        sys.exit(1)
