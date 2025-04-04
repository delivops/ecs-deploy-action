#!/usr/bin/env python3
import yaml
import json
import argparse
import sys

def generate_task_definition(yaml_file_path, aws_region, registry, image_name, tag):
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
    env_name = config.get('env', 'development')
    app_name = config.get('name', 'app')
    cpu = str(config.get('cpu', 256))
    memory = str(config.get('memory', 512))
    include_otel = config.get('include_otel_collector', False)
    otel_collector_ssm_path = config.get('otel_collector_ssm_path', 'adot-config-global.yaml')
    cpu_arch = config.get('cpu_arch', 'X86_64')
    
    # Get environment variables
    environment = []
    for env_var in config.get('env', []):
        for key, value in env_var.items():
            environment.append({
                "name": key,
                "value": value
            })
    
    # Get secrets
    secrets = []
    for secret in config.get('secrets', []):
        for key, value in secret.items():
            secrets.append({
                "name": key,
                "valueFrom": value
            })
    
    # Build the full image URI
    image_uri = f"{registry}/{image_name}:{tag}"
    
    # Create app container definition
    app_container = {
        "name": "app",
        "image": image_uri,
        "essential": True,
        "environment": environment,
        "secrets": secrets,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{env_name}/{app_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "app"
            }
        }
    }
    
    print(f"Setting container image to: {image_uri}")
    
    # Create the container definitions list
    container_definitions = [app_container]
    
    # Add the OpenTelemetry collector container if specified
    if include_otel:
        otel_container = {
            "name": "otel-collector",
            "image": "otel/opentelemetry-collector-contrib",
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
            "command": [
                "--config",
                "env:SSM_CONFIG"
            ],
            "secrets": [
                {
                    "name": "SSM_CONFIG",
                    "valueFrom": otel_collector_ssm_config
                }
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": f"/ecs/{env_name}/{app_name}",
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
        "family": f"{env_name}_{app_name}",
        "taskRoleArn": config.get('role_arn', ''),
        "executionRoleArn": config.get('role_arn', ''),
        "networkMode": "awsvpc",
        "requiresCompatibilities": [
            "FARGATE"
        ]
    }
    
    return task_definition

def parse_args():
    """Parse and validate command line arguments"""
    parser = argparse.ArgumentParser(description='Generate ECS task definition from YAML configuration')
    
    parser.add_argument('yaml_file', help='Path to the YAML configuration file')
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
            args.aws_region,
            args.registry,
            args.image_name,
            args.tag
        )
        
        # Write to the specified output file
        with open(args.output, 'w') as file:
            json.dump(task_definition, file, indent=2)
            
        print(f"Task definition successfully written to {args.output}")
        
    except Exception as e:
        print(f"Error generating task definition: {e}", file=sys.stderr)
        sys.exit(1)
