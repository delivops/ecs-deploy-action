[![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)](https://delivops.com)

# ECS Deploy Action

This GitHub composite action simplifies deploying applications to Amazon ECS by generating a task definition from a simple YAML configuration and handling the deployment process.

## Features

- Generates ECS task definitions from simplified YAML configurations
- Supports OpenTelemetry collector sidecars with custom or default AWS images
- Supports Fluent Bit log collector sidecars for advanced log routing
- Handles environment variables and secrets (including secret files)
- Configures proper logging with CloudWatch or Fluent Bit
- Supports custom CPU and memory allocations with ARM64/X86_64 architectures
- Supports health checks and custom port configurations
- Handles both ECR and public Docker registries
- Supports dry-run mode for testing
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
          aws_role: github_services # optional, defaults to 'github_services'
          dry_run: false # optional, defaults to false
          ecr_registry: true # optional, defaults to true
```

## YAML Configuration Format

Create a YAML configuration file with the following structure:

### Basic Configuration

```yaml
# Number of desired instances/replicas of this service
replica_count: 3

# CPU allocation (in CPU units: 256, 512, 1024, 2048, 4096)
cpu: 1024

# Memory allocation (in MB: 512, 1024, 2048, 3072, 4096, 5120, 6144, 7168, 8192)
memory: 2048

# CPU architecture (X86_64 or ARM64)
cpu_arch: X86_64

# IAM role ARN for both task execution and task role
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole

# Main port for the application
port: 8080

# Additional ports with custom names
additional_ports:
  - metrics: 9090
  - health: 8081

# Container command and entrypoint override
command: ["npm", "start"]
entrypoint: ["/usr/local/bin/docker-entrypoint.sh"]

# Health check configuration
health_check:
  command: "curl -f http://localhost:8080/health || exit 1"
  interval: 30 # seconds between health checks
  timeout: 5 # seconds to wait for health check
  retries: 3 # number of consecutive failures before unhealthy
  start_period: 60 # seconds to wait before first health check
```

### Environment Variables and Secrets

```yaml
# Environment variables passed to the container
envs:
  - NODE_ENV: production
  - API_VERSION: v1
  - LOG_LEVEL: info
  - MAX_CONNECTIONS: 100 # Integer - will be converted to "100"
  - ENABLE_METRICS: true # Boolean - will be converted to "True"

# Secrets from AWS Secrets Manager (classic format)
# Format: ENV_VAR_NAME: secret-arn
# The ENV_VAR_NAME must match a key that exists in the AWS secret
secrets:
  - DATABASE_PASSWORD: arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-db-password
  - API_KEY: arn:aws:secretsmanager:us-east-1:123456789012:secret:external-api-key

# Alternative: New grouped format for secrets
# The values listed must be keys that exist in the specified AWS secret
secrets_envs:
  - id: arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123
    values:
      - DATABASE_PASSWORD # This key must exist in the secret
      - API_KEY # This key must exist in the secret

# Secret files (downloaded to /etc/secrets/ during container startup)
secret_files:
  - ssl-certificate
  - private-key
  - config-file
# Example: If your AWS Secrets Manager secret contains:
# {
#   "DATABASE_PASSWORD": "mypassword123",
#   "API_KEY": "sk-1234567890abcdef",
#   "JWT_SECRET": "supersecretjwtkey"
# }
#
# Then you can reference these keys using either format:
#
# Classic format:
# secrets:
#   - DATABASE_PASSWORD: arn:aws:secretsmanager:us-east-1:123456789012:secret:my-app-secrets
#   - API_KEY: arn:aws:secretsmanager:us-east-1:123456789012:secret:my-app-secrets
#
# Grouped format:
# secrets_envs:
#   - id: arn:aws:secretsmanager:us-east-1:123456789012:secret:my-app-secrets
#     values:
#       - DATABASE_PASSWORD
#       - API_KEY
#       - JWT_SECRET
```

### OpenTelemetry Configuration

```yaml
# OpenTelemetry collector configuration
otel_collector:
  # Custom OTel collector image (respects ecr_registry setting)
  image_name: "my-custom-otel-collector:v1.0.0"

  # For custom images: configuration file name (located in /conf/ directory)
  extra_config: "otel-config.yaml"

  # For default AWS image: SSM parameter name containing the configuration
  ssm_name: "my-app-otel-config.yaml"

  # Optional: Custom metrics port (default: 8080)
  metrics_port: 8888

  # Optional: Custom metrics path (default: /metrics)
  metrics_path: "/custom/metrics"
# Example: Default AWS OTEL Collector (uses SSM config)
# otel_collector:
#   ssm_name: "production-otel-config.yaml"

# Example: Custom OTEL image
# otel_collector:
#   image_name: "my-company/otel-collector:v2.0.0"
#   extra_config: "custom-config.yaml"
#   metrics_port: 9090
```

### Fluent Bit Logging Configuration

```yaml
# Fluent Bit log collector configuration
fluent_bit_collector:
  # Fluent Bit image name (always uses ECR registry)
  image_name: "fluent-bit:2.1.0"

  # Extra configuration file name (located in extra/ directory)
  extra_config: "custom-fluent-bit.conf"

  # Enable ECS log metadata in Fluent Bit
  ecs_log_metadata: "true"
```

## Inputs

| Input              | Description                                    | Required | Default           |
| ------------------ | ---------------------------------------------- | -------- | ----------------- |
| `environment`      | The environment to deploy to                   | Yes      |                   |
| `ecs_service`      | The name of the ECS service                    | Yes      |                   |
| `image_name`       | The name of the Docker image                   | Yes      |                   |
| `tag`              | The tag of the Docker image                    | Yes      |                   |
| `task_config_yaml` | Path to the YAML configuration file            | Yes      |                   |
| `aws_account_id`   | The AWS account ID                             | Yes      |                   |
| `aws_region`       | The AWS region                                 | Yes      |                   |
| `ecs_cluster`      | The name of the ECS cluster                    | Yes      |                   |
| `aws_role`         | The AWS IAM role to assume                     | No       | `github_services` |
| `dry_run`          | Whether to perform a dry run                   | No       | `false`           |
| `ecr_registry`     | Whether to use ECR registry for main container | No       | `true`            |

## Important Note on AWS Credentials

This action includes steps to configure AWS credentials and interact with ECR and ECS. In most GitHub Actions workflows, it's best practice to configure AWS credentials at the workflow level rather than within each action to avoid credential scope issues and provide better security control.

## How It Works

1. **AWS Authentication**: Configures AWS credentials using the specified IAM role
2. **ECR Login**: Authenticates with Amazon ECR for private image access
3. **Registry Configuration**: Determines whether to use ECR or public registries based on settings
4. **Task Definition Generation**: Reads your YAML configuration and generates a complete ECS task definition with:
   - Main application container with proper resource allocation
   - Optional OpenTelemetry collector sidecar for observability
   - Optional Fluent Bit sidecar for advanced log routing
   - Init containers for secret file downloads if needed
   - Proper networking, health checks, and dependencies
5. **Deployment**: Deploys the generated task definition to your ECS service
6. **Verification**: Confirms the deployment succeeded and the new task definition is active

## Registry Behavior

- **Main Container**: Uses ECR registry when `ecr_registry: true` (default), otherwise uses public registry
- **Sidecar Containers**: OpenTelemetry and Fluent Bit containers always use ECR registry for security
- **Default Images**: AWS-provided OTEL collector uses public ECR by default

## Advanced Features

### Secret Management

- **Environment Secrets**: Inject secrets from AWS Secrets Manager as environment variables
  - **Important**: The environment variable names you specify must match the actual keys stored in your AWS Secrets Manager secret
  - **Classic Format**: Map each environment variable to its specific secret ARN
  - **Grouped Format**: Reference multiple keys from the same secret ARN
- **Secret Files**: Download secret files to `/etc/secrets/` during container startup using init containers
- **Multiple Formats**: Support both classic and grouped secret configurations

### Observability

- **OpenTelemetry**: Built-in support for OTEL collectors with custom or AWS-managed configurations
- **Logging**: Choose between direct CloudWatch logging or advanced Fluent Bit routing
- **Health Checks**: Configure custom health check commands with retry logic

### Container Architecture

- **Multi-Architecture**: Support for both X86_64 and ARM64 (Graviton) processors
- **Port Management**: Configure main application port plus additional named ports
- **Resource Allocation**: Flexible CPU and memory configurations following AWS Fargate constraints

### Deployment Options

- **Dry Run**: Test configuration generation without actual deployment
- **Registry Flexibility**: Use ECR for private images or public registries for open-source images
- **Replica Control**: Specify desired instance count directly in YAML configuration

## Examples

See the `examples/` directory for complete configuration examples:

- `full-example-available.yaml`: Comprehensive example with all available options
- `otel-custom-image.yaml`: Custom OpenTelemetry collector setup
- `otel-default-image.yaml`: Default AWS OTEL collector setup
- `private-dkr-image.yaml`: Private Docker registry configuration
- `public-image.yaml`: Public Docker image configuration

## Development

If you need to modify the task definition generation, edit the Python script at `scripts/generate_task_def.py`. The script uses argparse for clear parameter parsing and validation, and supports all the advanced features documented above.
