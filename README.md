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
- **EC2 and Fargate launch type support**
- **Linux parameters configuration** (init process, capabilities, shared memory, devices)

## Deployment Types

### ECS Services
Deploy long-running services with automatic health checks and zero-downtime deployments.

### Scheduled Tasks
Deploy EventBridge-scheduled tasks that run on a schedule. Infrastructure (EventBridge rules, network config, IAM roles) is managed in Terraform, while the GitHub Action handles task definition updates.

### Triggerable Tasks
Deploy standalone task definitions that can be triggered manually or by external systems (e.g., Lambda, Step Functions, other workflows). Only registers the task definition without any EventBridge configuration.

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `environment` | The environment to deploy to | Yes | - |
| `deployment_type` | Deployment type: `service`, `scheduled_task`, or `triggerable_task` | No | `service` |
| `ecs_service` | The name of the ECS service (required when `deployment_type=service`) | No | - |
| `task_name` | The name of the task (required when `deployment_type=scheduled_task` or `triggerable_task`) | No | - |
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

### Deploy Triggerable Task

```yaml
name: Deploy Triggerable Task

on:
  push:
    branches: [main]

jobs:
  deploy:
    name: Deploy Triggerable Task
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Deploy Triggerable Task
        uses: delivops/ecs-deploy-action@v1
        with:
          environment: production
          deployment_type: triggerable_task
          task_name: batch-job
          ecs_cluster: production
          image_name: batch-job
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
- [EC2 launch type support](./docs/ec2-launch-type.md) (with bridge/host network modes)
- [Linux parameters](./docs/linux-parameters.md) (init process, capabilities, shared memory, devices)
- Multi-service YAML configuration
- And more

### Multi-Service YAML

You can use a single YAML file for multiple services by defining shared defaults at the top level and service-specific overrides in a `services_overrides` section:

```yaml
# Base configuration (applied to all services)
cpu: 256
memory: 512
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
port: 8080
envs:
  - LOG_LEVEL: info
  - SHARED_VAR: shared_value

# Service-specific overrides
services_overrides:
  api-service:
    cpu: 1024
    memory: 2048
    envs:
      - API_MODE: "true"
  worker-service:
    cpu: 512
    memory: 1024
    port: null  # Remove port for workers
    envs:
      - WORKER_MODE: "true"
```

**Merge behavior:**
| Field Type | Behavior |
|------------|----------|
| Scalars (`cpu`, `memory`, `port`) | Override replaces base value |
| Arrays (`envs`, `secrets`, `command`) | Service values are appended to base |
| Objects (`health_check`, `otel_collector`) | Service object completely replaces base |
| Null values | Removes the field from configuration |

The service name passed to the action (via `ecs_service` or `task_name`) determines which overrides are applied. Services not listed in `services_overrides` use the base configuration.

## Architecture

For scheduled tasks, the action:
1. Generates an ECS task definition from your YAML config
2. Registers the new task definition with ECS
3. Updates the EventBridge rule target to use the new task definition

For triggerable tasks, the action:
1. Generates an ECS task definition from your YAML config
2. Registers the new task definition with ECS

All infrastructure (EventBridge rules, schedules, network configuration, IAM roles) remains managed in your Terraform code, ensuring clear separation of concerns between infrastructure and application deployments.

## License

This project is maintained by [DelivOps](https://delivops.com).
