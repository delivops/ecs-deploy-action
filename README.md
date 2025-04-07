# ECS Deploy Action

This GitHub composite action simplifies deploying applications to Amazon ECS by generating a task definition from a simple YAML configuration and handling the deployment process.

## Features

- Generates ECS task definitions from simplified YAML configurations
- Supports optional OpenTelemetry collector sidecars
- Handles environment variables and secrets
- Configures proper logging
- Supports custom CPU and memory allocations
- Verifies successful deployments

## Usage

### In Your Workflow

```yaml
name: Deploy Application
on:
  push:
    branches:
      - main

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      
      # Build and tag your image here  
      
      - name: Deploy to ECS
        uses: delivops/ecs-deploy-action@main
        with:
          environment: production
          ecs_service: my-service
          image_name: my-app
          tag: ${{ github.sha }}
          task_config_yaml: apps/my-service/.aws/production.yaml
          ecs_cluster: ${{ vars.ECS_CLUSTER }}
          aws_region: ${{ vars.AWS_REGION }}
          aws_account_id: ${{ secrets.AWS_ACCOUNT_ID }}
```

## YAML Configuration Format

Create a YAML configuration file with the following structure:

```yaml
name: my_app                    # Application name
cpu: 1024                       # CPU units
memory: 8192                    # Memory in MB
include_otel_collector: true    # Include OpenTelemetry collector sidecar

# cpu_arch: X86_64                # Optional: CPU architecture (default: X86_64)
# otel_collector_ssm_config: otel-config.yaml  # Optional: Custom OTEL config name on SSM

role_arn: arn:aws:iam::123456789:role/appTaskRole

# Environment variables
env:
  - KEY1: value1
  - KEY2: value2

# Secrets from AWS Secrets Manager
secrets:
  - SECRET_NAME: arn:aws:secretsmanager:region:account:secret:path
```

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `ecs_service` | The name of the ECS service | Yes | |
| `image_name` | The name of the Docker image | Yes | |
| `tag` | The tag of the Docker image | Yes | |
| `task_config_yaml` | Path to the YAML configuration file | Yes | |
| `aws_account_id` | The AWS account ID | Yes | |
| `aws_region` | The AWS region | Yes | |
| `ecs_cluster` | The name of the ECS cluster | Yes | |
| `aws_role` | The AWS IAM role to assume | No | `github_services` |

## Important Note on AWS Credentials

This action includes steps to configure AWS credentials and interact with ECR and ECS. In most GitHub Actions workflows, it's best practice to configure AWS credentials at the workflow level rather than within each action to avoid credential scope issues and provide better security control.

## How It Works

1. The action reads your YAML configuration file
2. Generates a proper ECS task definition JSON
3. Injects your Docker image with the specified tag
4. Deploys the task definition to your ECS service
5. Verifies that the deployment succeeded

## Development

If you need to modify the task definition generation, edit the Python script at `scripts/generate_task_def.py`. The script uses argparse for clear parameter parsing and validation.