# ECS Deploy Action

This GitHub Action automates the deployment of containerized applications to Amazon ECS (Elastic Container Service). It streamlines the process of updating ECS services with new container images, handling the deployment process in a reliable and efficient manner.

### Key Features
- Automated deployment to Amazon ECS
- Supports task definition updates
- Handles service updates with zero-downtime deployment
- Configurable deployment parameters
- Integration with GitHub Actions workflow

### Example Usage

```yaml
name: Deploy to ECS

on:
  push:
    branches: [ main ]
    paths:
      - '**'
      - '!.github/**'

jobs:
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout
      uses: actions/checkout@v3
      
    - name: Configure AWS credentials
      uses: aws-actions/configure-aws-credentials@v2
      with:
        aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
        aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        aws-region: us-east-1
        
    - name: Deploy to ECS
      uses: ./  # Points to the local action in this repository
      with:
        cluster: my-ecs-cluster
        service: my-ecs-service
        container-name: my-container
        image: my-ecr-repo:latest
        task-definition: task-definition.json
        aws-region: us-east-1
```

## Configuration Reference

This section provides a comprehensive reference for all available configuration options and their usage.

<start dynamic>

### Complete YAML Configuration

```yaml
# Example of a complete YAML configuration with all available options
service_name: my-service  # Required: Name of the ECS service
cluster_name: my-cluster  # Required: Name of the ECS cluster

# Container configuration
container_name: app       # Name of the main container
cpu: 1024                 # CPU units (256, 512, 1024, 2048, 4096)
memory: 2048              # Memory in MB
cpu_arch: X86_64          # CPU architecture (X86_64 or ARM64)

# Image configuration
image: nginx:latest       # Container image
port: 80                  # Main container port
additional_ports:         # Additional container ports
  - metrics: 9090
  - debug: 4000

# Environment variables
envs:
  - NODE_ENV: production
  - LOG_LEVEL: info
  - API_URL: https://api.example.com

# Secrets configuration (legacy format)
secrets:
  - DB_PASSWORD: arn:aws:secretsmanager:region:account:secret:db-password
  - API_KEY: arn:aws:secretsmanager:region:account:secret:api-key

# OR use the new grouped secrets format
secrets_envs:
  - id: arn:aws:secretsmanager:region:account:secret:app-secrets
    values: [DB_PASSWORD, API_KEY, JWT_SECRET]

# Health check configuration
health_check:
  command: ["CMD-SHELL", "curl -f http://localhost/health || exit 1"]
  interval: 30
  timeout: 5
  retries: 3
  start_period: 60

# OpenTelemetry configuration (optional)
otel_collector:
  enabled: true
  image_name: public.ecr.aws/aws-observability/aws-otel-collector:latest
  metrics_port: 8888
  metrics_path: /metrics
  extra_config: config/otel-config.yaml  # Path to custom config
  ssm_name: /app/otel-config             # SSM parameter for config

# Fluent Bit configuration (optional)
fluent_bit_collector:
  enabled: true
  image_name: public.ecr.aws/aws-observability/aws-for-fluent-bit:latest
  extra_config: config/fluent-bit.conf
  ecs_log_metadata: true

# Task execution role
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole

# Task definition overrides
task_definition:
  networkMode: awsvpc
  requiresCompatibilities: ["FARGATE"]
  executionRoleArn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
  volumes:
    - name: app-storage
      efsVolumeConfiguration:
        fileSystemId: fs-12345678
        transitEncryption: ENABLED
```

### Generated JSON Task Definition

When processed by the Python script, the above YAML generates the following ECS task definition:

```json
{
  "family": "my-service",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "arn:aws:iam::123456789012:role/ecsTaskExecutionRole",
  "containerDefinitions": [
    {
      "name": "app",
      "image": "nginx:latest",
      "cpu": 1024,
      "memory": 2048,
      "portMappings": [
        {
          "name": "default",
          "containerPort": 80,
          "hostPort": 80,
          "protocol": "tcp",
          "appProtocol": "http"
        },
        {
          "name": "metrics",
          "containerPort": 9090,
          "hostPort": 9090,
          "protocol": "tcp",
          "appProtocol": "http"
        },
        {
          "name": "debug",
          "containerPort": 4000,
          "hostPort": 4000,
          "protocol": "tcp",
          "appProtocol": "http"
        }
      ],
      "essential": true,
      "environment": [
        {"name": "NODE_ENV", "value": "production"},
        {"name": "LOG_LEVEL", "value": "info"},
        {"name": "API_URL", "value": "https://api.example.com"}
      ],
      "secrets": [
        {
          "name": "DB_PASSWORD",
          "valueFrom": "arn:aws:secretsmanager:region:account:secret:db-password:DB_PASSWORD::"
        },
        {
          "name": "API_KEY",
          "valueFrom": "arn:aws:secretsmanager:region:account:secret:api-key:API_KEY::"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/my-cluster/my-service",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "/default"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost/health || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 60
      }
    },
    {
      "name": "aws-otel-collector",
      "image": "public.ecr.aws/aws-observability/aws-otel-collector:latest",
      "essential": true,
      "command": [
        "--config=/etc/ecs/ecs-default-config.yaml"
      ],
      "portMappings": [
        {
          "containerPort": 8888,
          "protocol": "tcp"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/my-cluster/my-service",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "otel"
        }
      }
    },
    {
      "name": "fluent-bit",
      "image": "public.ecr.aws/aws-observability/aws-for-fluent-bit:latest",
      "essential": true,
      "firelensConfiguration": {
        "type": "fluentbit"
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/my-cluster/my-service",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "fluent-bit"
        }
      }
    }
  ],
  "volumes": [
    {
      "name": "app-storage",
      "efsVolumeConfiguration": {
        "fileSystemId": "fs-12345678",
        "transitEncryption": "ENABLED"
      }
    }
  ]
}
```

<end dynamic>

## Getting Started

### Example Usage

Here's a basic example of how to use this GitHub Action in your workflow:

### Example of GitHub Action Usage
```yaml
- name: Deploy to ECS
  uses: delivops/ecs-deploy-action@v1
  with:
    cluster: production-cluster
    service: web-service
    container-name: web-app
    image: 123456789012.dkr.ecr.region.amazonaws.com/web-app:latest
    task-definition: task-definition.json
    aws-region: us-east-1
    force-new-deployment: true
```

This example demonstrates how to use the action in a GitHub workflow, specifying the ECS cluster, service, container details, and deployment configuration.
