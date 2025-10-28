[![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)](https://delivops.com)

# ECS Deploy Action

This GitHub Action automates the deployment of containerized applications to Amazon ECS (Elastic Container Service). It streamlines the process of updating ECS services and scheduled tasks with new container images, handling the deployment process in a reliable and efficient manner.

### Key Features

- Automated deployment to Amazon ECS Services and Scheduled Tasks
- Simplified YAML-based task definition configuration
- Zero-downtime deployment for ECS services
- EventBridge scheduled task support
- Configurable deployment parameters
- Support for OpenTelemetry and Fluent Bit sidecars
- Secrets management integration
- Integration with GitHub Actions workflow

## Deployment Types

### ECS Services
Deploy long-running services with automatic health checks and zero-downtime deployments.

### Scheduled Tasks
Deploy EventBridge-scheduled tasks that run on a schedule. Infrastructure (EventBridge rules, network config, IAM roles) is managed in Terraform, while the GitHub Action handles task definition updates.

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `environment` | The environment to deploy to | Yes | - |
| `deployment_type` | Deployment type: `service` or `scheduled_task` | No | `service` |
| `ecs_service` | The name of the ECS service (required when `deployment_type=service`) | No | - |
| `task_name` | The name of the scheduled task (required when `deployment_type=scheduled_task`) | No | - |
| `image_name` | The name of the Docker image | Yes | - |
| `tag` | The tag of the Docker image | Yes | - |
| `task_config_yaml` | Path to the YAML file containing task configuration | Yes | - |
| `aws_account_id` | The AWS account ID | Yes | - |
| `aws_region` | The AWS region | Yes | - |
| `ecs_cluster` | The name of the ECS cluster | Yes | - |
| `aws_role` | The AWS IAM role to assume | No | `github_services` |
| `dry_run` | Whether to perform a dry run | No | `false` |
| `ecr_registry` | Use ECR registry for main container image | No | `true` |

## Example Usage

### Deploy to ECS Service

```yaml
name: Deploy to ECS Service

on:
  push:
    branches: [main]

jobs:
  deploy:
    name: Deploy Service
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Deploy to ECS Service
        uses: delivops/ecs-deploy-action@v1
        with:
          environment: production
          deployment_type: service
          ecs_service: my-api
          ecs_cluster: production
          image_name: my-api
          tag: ${{ github.sha }}
          task_config_yaml: config/task.yaml
          aws_account_id: ${{ secrets.AWS_ACCOUNT_ID }}
          aws_region: us-east-1
```

### Deploy to Scheduled Task

```yaml
name: Deploy Scheduled Task

on:
  push:
    branches: [main]
    paths:
      - 'src/**'
      - 'config/**'

jobs:
  deploy:
    name: Deploy Scheduled Task
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Deploy Scheduled Task
        uses: delivops/ecs-deploy-action@v1
        with:
          environment: production
          deployment_type: scheduled_task
          task_name: data-processor
          ecs_cluster: production
          image_name: data-processor
          tag: ${{ github.sha }}
          task_config_yaml: config/task.yaml
          aws_account_id: ${{ secrets.AWS_ACCOUNT_ID }}
          aws_region: us-east-1
```

## Task Configuration

The action uses a simplified YAML configuration file for task definitions. See the [examples](./examples/) directory and [documentation](./docs/) for detailed configuration options including:

- Resource allocation (CPU, memory)
- Environment variables and secrets
- Health checks
- Port mappings
- OpenTelemetry and Fluent Bit integration
- And more

## Architecture

For scheduled tasks, the action:
1. Generates an ECS task definition from your YAML config
2. Registers the new task definition with ECS
3. Updates the EventBridge rule target to use the new task definition

All infrastructure (EventBridge rules, schedules, network configuration, IAM roles) remains managed in your Terraform code, ensuring clear separation of concerns between infrastructure and application deployments.

## License

This project is maintained by [DelivOps](https://delivops.com).
