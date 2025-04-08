#!/usr/bin/env python3
import yaml
import json
import argparse
import sys
import boto3

def get_aws_account_id():
    """
    Fetch the AWS Account ID of the current IAM user or role.
    
    Returns:
        str: The AWS account ID.
    """
    sts_client = boto3.client('sts')
    response = sts_client.get_caller_identity()
    return response['Account']

def get_secret_values(secret_arn, aws_region):
    """
    Fetch all key-value pairs from AWS Secrets Manager based on ARN and return as a list of dictionaries.
    
    Args:
        secret_arn (str): ARN of the secret in Secrets Manager.
        aws_region (str): AWS region where the secret is stored.
    
    Returns:
        list: List of dictionaries containing name-value pairs for ECS secrets.
    """
    # Initialize the Secrets Manager client
    client = boto3.client('secretsmanager', region_name=aws_region)
    
    # Get the AWS Account ID
    account_id = get_aws_account_id()
    
    try:
        # Get the secret value using the ARN
        response = client.get_secret_value(SecretId=secret_arn)
        
        # Secrets Manager returns either 'SecretString' or 'SecretBinary'
        if 'SecretString' in response:
            secret = response['SecretString']
            secrets_dict = json.loads(secret)
            
            # Format the secrets for ECS (using ARN structure)
            formatted_secrets = []
            for key, value in secrets_dict.items():
                formatted_secrets.append({
                    "name": key,
                    "valueFrom": f"arn:aws:secretsmanager:{aws_region}:{account_id}:secret:{secret_arn.split(':')[-1]}-{key}::{key}"
                })
            return formatted_secrets
        
        else:
            # Handle the case where the secret is binary
            raise ValueError("Secret is stored as binary, which is unsupported in this example")
    
    except Exception as e:
        print(f"Error fetching secrets: {e}")
        return []

def generate_task_definition(yaml_file_path, cluster_name, aws_region, registry, image_name, tag):
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
    include_otel = config.get('include_otel_collector', False)
    otel_collector_ssm_path = config.get('otel_collector_ssm_path', 'adot-config-global.yaml')
    cpu_arch = config.get('cpu_arch', 'X86_64')
    command = config.get('command', [])
    entrypoint = config.get('entrypoint', [])
    

    # Get environment variables
    environment = []
    for env_var in config.get('env_variables', []):
        for key, value in env_var.items():
            environment.append({
                "name": key,
                "value": value
            })
    
    # Get secrets using the ARN from config
    secrets = []
    secret_arn = config.get('secret_arn', '')
    if secret_arn:
        secrets.extend(get_secret_values(secret_arn, aws_region))
    
    # Check for SSM file configuration
    env_file_arn = config.get('env_file_arn', '')
    has_env_file = bool(env_file_arn)
    
    # Create shared volume for SSM files if needed
    volumes = []
    if has_env_file:
        volumes.append({
            "name": "shared-volume",
            "host": {}
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
        "volumes": volumes,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "/"
            }
        }
    }

    # Handle port configurations
    main_port = config.get('port')
    additional_ports = config.get('additional_ports', [])
    
    port_mappings = []
    
    # Add main port if specified
    if main_port:
        port_mapping = {
            "name": f"{cluster_name}_{app_name}",
            "containerPort": main_port,
            "hostPort": main_port,
            "protocol": "tcp",
            "appProtocol": "http"
        }
        port_mappings.append(port_mapping)
    
    # Add additional ports if specified
    for i, port in enumerate(additional_ports):
        # Start index from 2 for additional ports
        index = i + 2
        port_mapping = {
            "name": f"{cluster_name}_{app_name}_{index}",
            "containerPort": port,
            "hostPort": port,
            "protocol": "tcp",
            "appProtocol": "http"
        }
        port_mappings.append(port_mapping)
    
    if port_mappings:
        app_container["portMappings"] = port_mappings
    
    # Add mount points if using shared volume
    if has_env_file:
        app_container["mountPoints"] = [
            {
                "sourceVolume": "shared-volume",
                "containerPath": "/etc/secrets"
            }
        ]
        
        # Add dependency on init containers
        app_container["dependsOn"] = [
            {
                "containerName": "init-container-for-env-file",
                "condition": "SUCCESS"
            }
        ]
    
    print(f"Setting container image to: {image_uri}")
    
     # Create the container definitions list
    container_definitions = []
    
    # Create init container for env file if needed
    if has_env_file:
        # Extract the secret name from the ARN
        secret_name = env_file_arn.split(':')[-1].split('/')[-1] if env_file_arn else f"{cluster_name}_{app_name}_secret"
        file_name = f"{cluster_name}_{app_name}_secret"
        
        init_container = {
            "name": "init-container-for-env-file",
            "image": "amazon/aws-cli",
            "essential": False,
            "entryPoint": ["/bin/sh"],
            "command": [
                "-c",
                f"aws secretsmanager get-secret-value --secret-id {secret_name} --region {aws_region} --query SecretString --output text > /etc/secrets/{file_name}"
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
                    "awslogs-region": aws_region
                }
            }
        }
        container_definitions.append(init_container)
    
    # Add the main application container
    container_definitions.append(app_container)
    # Add the OpenTelemetry collector container if specified
    if include_otel:
        otel_container = {
            "name": "otel-collector",
            "image": "public.ecr.aws/aws-observability/aws-otel-collector:v0.43.1",
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
                    "valueFrom": otel_collector_ssm_path
                }
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                    "awslogs-region": aws_region
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
        "command": command,
        "entryPoint": entrypoint,
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
            
        print(f"Task definition successfully written to {args.output}")
        
    except Exception as e:
        print(f"Error generating task definition: {e}", file=sys.stderr)
        sys.exit(1)
