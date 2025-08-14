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
          aws_account_id: ${{ vars.AWS_ACCOUNT_ID }}
          aws_region: ${{ vars.AWS_REGION }}
```

### Configuration File Example

Create a YAML configuration file (e.g., `production.yaml`):

```yaml
name: my-app
replica_count: 2
cpu: 512
memory: 1024
cpu_arch: X86_64
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole

# Environment variables
envs:
  - NODE_ENV: production
  - PORT: 8080
  - LOG_LEVEL: info

# Port configuration
port: 8080
additional_ports:
  - metrics: 9090

# Health check
health_check:
  command: "curl -f http://localhost:8080/health || exit 1"
  interval: 30
  timeout: 5
  retries: 3
  start_period: 10

# Secrets from AWS Secrets Manager
secrets_envs:
  - id: arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-xyz123
    values:
      - DATABASE_PASSWORD
      - API_KEY

# Optional: OpenTelemetry collector
otel_collector:
  image_name: custom-otel-collector
  extra_config: otel-config.yaml
  metrics_port: 8080
  metrics_path: /metrics
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

### Task Definition Generator

The core logic is in `scripts/generate_task_def.py`, which provides:

- **Type Safety**: Full type hints and dataclasses for better code structure
- **Error Handling**: Custom exceptions and comprehensive validation
- **Structured Logging**: Configurable logging levels with detailed output
- **Code Organization**: Separated concerns into builder classes
- **Configuration Validation**: Validates CPU/memory combinations per AWS Fargate requirements
- **CLI Enhancements**: Better help text, validation-only mode, configurable output

#### Setup

1. Activate the virtual environment:
   ```bash
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

#### Usage

```bash
# Basic usage
python scripts/generate_task_def.py config.yaml cluster-name us-east-1 
  registry.ecr.amazonaws.com registry.ecr.amazonaws.com 
  my-app latest my-service

# Validation only
python scripts/generate_task_def.py config.yaml cluster-name us-east-1 
  registry.ecr.amazonaws.com registry.ecr.amazonaws.com 
  my-app latest my-service --validate-only

# Debug mode
python scripts/generate_task_def.py config.yaml cluster-name us-east-1 
  registry.ecr.amazonaws.com registry.ecr.amazonaws.com 
  my-app latest my-service --log-level DEBUG

# Custom output file
python scripts/generate_task_def.py config.yaml cluster-name us-east-1 
  registry.ecr.amazonaws.com registry.ecr.amazonaws.com 
  my-app latest my-service --output custom-task-def.json
```

### Testing

The test suite automatically validates all example configurations:

1. Reads each YAML file from the `examples/` directory
2. Executes the `scripts/generate_task_def.py` script with default parameters
3. Compares the output against expected JSON files in `tests/expected_outputs/`
4. Creates expected JSON files if they don't exist

#### Run Tests

```bash
# Run all tests
python tests/test.py
```

#### CI/CD Integration

The GitHub Actions workflow (`.github/workflows/test-and-update.yml`) automatically:
- Runs tests on all example configurations
- Updates expected outputs when configurations change
- Validates that all examples generate valid task definitions

### File Structure

```
├── action.yml                    # GitHub Action definition
├── examples/                     # Example YAML configurations
├── scripts/
│   └── generate_task_def.py     # Core task definition generator
├── tests/
│   ├── test.py                  # Test runner
│   └── expected_outputs/        # Expected JSON outputs
├── requirements.txt             # Python dependencies
└── .venv/                       # Python virtual environment
```

## Contributing

1. Make changes to the script or examples
2. Run tests to ensure compatibility: `python tests/test.py`
3. Commit changes - tests will run automatically in CI
4. Expected outputs will be updated automatically if needed

## License

This project is licensed under the MIT License.

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

## 📋 Complete YAML Configuration Example

## 📋 Complete YAML Configuration Example

<!-- AUTO-GENERATED-YAML-START -->
```yaml
replica_count: 3
cpu: 1024
memory: 2048
cpu_arch: X86_64
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
port: 8080
additional_ports:
- metrics: 9090
- health: 8081
- admin: 8082
command:
- npm
- start
entrypoint:
- /usr/local/bin/docker-entrypoint.sh
health_check:
  command: curl -f http://localhost:8080/health || exit 1
  interval: 30
  timeout: 5
  retries: 3
  start_period: 60
envs:
- NODE_ENV: production
- API_VERSION: v1
- LOG_LEVEL: info
- MAX_CONNECTIONS: 100
- TIMEOUT_SECONDS: 30
- ENABLE_METRICS: true
- DEBUG_MODE: false
secrets_envs:
- id: arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123
  values:
  - DATABASE_PASSWORD
  - API_KEY
  - JWT_SECRET
- id: arn:aws:secretsmanager:us-east-1:123456789012:secret:external-services-def456
  values:
  - STRIPE_API_KEY
  - SENDGRID_API_KEY
secret_files:
- ssl-certificate
- private-key
- config-file
fluent_bit_collector:
  image_name: fluent-bit:2.1.0
  extra_config: custom-fluent-bit.conf
  ecs_log_metadata: 'true'
otel_collector:
  image_name: my-custom-otel-collector:v1.0.0
  extra_config: otel-config.yaml
  ssm_name: my-app-otel-config.yaml
  metrics_port: 8888
  metrics_path: /custom/metrics
```
<!-- AUTO-GENERATED-YAML-END -->

## 🔧 Generated Task Definition

## 🔧 Generated Task Definition

<!-- AUTO-GENERATED-TASK-DEF-START -->
```json
{
  "containerDefinitions": [
    {
      "name": "init-container-for-secret-files",
      "image": "amazon/aws-cli",
      "essential": false,
      "entryPoint": [
        "/bin/sh"
      ],
      "command": [
        "-c",
        "for secret in ${SECRET_FILES//,/ }; do echo \"Fetching $secret...\"; aws secretsmanager get-secret-value --secret-id $secret --region $AWS_REGION --query SecretString --output text > /etc/secrets/$secret; if [ $? -eq 0 ] && [ -s /etc/secrets/$secret ]; then echo \"\u2705 Successfully saved $secret to /etc/secrets/$secret\"; else echo \"\u274c Failed to save $secret\" >&2; exit 1; fi; done"
      ],
      "environment": [
        {
          "name": "SECRET_FILES",
          "value": "ssl-certificate,private-key,config-file"
        },
        {
          "name": "AWS_REGION",
          "value": "us-east-1"
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
          "awslogs-group": "/ecs/production-cluster/my-service",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ssm-file-downloader"
        }
      }
    },
    {
      "name": "app",
      "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-awesome-app:latest",
      "essential": true,
      "environment": [
        {
          "name": "NODE_ENV",
          "value": "production"
        },
        {
          "name": "API_VERSION",
          "value": "v1"
        },
        {
          "name": "LOG_LEVEL",
          "value": "info"
        },
        {
          "name": "MAX_CONNECTIONS",
          "value": "100"
        },
        {
          "name": "TIMEOUT_SECONDS",
          "value": "30"
        },
        {
          "name": "ENABLE_METRICS",
          "value": "True"
        },
        {
          "name": "DEBUG_MODE",
          "value": "False"
        }
      ],
      "command": [
        "npm",
        "start"
      ],
      "entryPoint": [
        "/usr/local/bin/docker-entrypoint.sh"
      ],
      "secrets": [
        {
          "name": "DATABASE_PASSWORD",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123:DATABASE_PASSWORD::"
        },
        {
          "name": "API_KEY",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123:API_KEY::"
        },
        {
          "name": "JWT_SECRET",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123:JWT_SECRET::"
        },
        {
          "name": "STRIPE_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:external-services-def456:STRIPE_API_KEY::"
        },
        {
          "name": "SENDGRID_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:external-services-def456:SENDGRID_API_KEY::"
        }
      ],
      "logConfiguration": {
        "logDriver": "awsfirelens",
        "options": {}
      },
      "healthCheck": {
        "command": [
          "CMD-SHELL",
          "curl -f http://localhost:8080/health || exit 1"
        ],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 60
      },
      "portMappings": [
        {
          "name": "default",
          "containerPort": 8080,
          "hostPort": 8080,
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
          "name": "health",
          "containerPort": 8081,
          "hostPort": 8081,
          "protocol": "tcp",
          "appProtocol": "http"
        },
        {
          "name": "admin",
          "containerPort": 8082,
          "hostPort": 8082,
          "protocol": "tcp",
          "appProtocol": "http"
        }
      ],
      "mountPoints": [
        {
          "sourceVolume": "shared-volume",
          "containerPath": "/etc/secrets"
        }
      ],
      "dependsOn": [
        {
          "containerName": "init-container-for-secret-files",
          "condition": "SUCCESS"
        },
        {
          "containerName": "fluent-bit",
          "condition": "START"
        }
      ]
    },
    {
      "name": "fluent-bit",
      "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/fluent-bit:2.1.0",
      "essential": true,
      "environment": [
        {
          "name": "SERVICE_NAME",
          "value": "my-service"
        },
        {
          "name": "ENV",
          "value": "production-cluster"
        }
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
          "awslogs-group": "/ecs/production-cluster/my-service",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "fluentbit"
        }
      },
      "firelensConfiguration": {
        "type": "fluentbit",
        "options": {
          "config-file-type": "file",
          "config-file-value": "extra/custom-fluent-bit.conf",
          "enable-ecs-log-metadata": "true"
        }
      }
    },
    {
      "name": "otel-collector",
      "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-custom-otel-collector:v1.0.0",
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
      "essential": true,
      "command": [
        "--config",
        "/conf/otel-config.yaml"
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/production-cluster/my-service",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "otel-collector"
        }
      },
      "environment": [
        {
          "name": "METRICS_PATH",
          "value": "/custom/metrics"
        },
        {
          "name": "METRICS_PORT",
          "value": "8888"
        },
        {
          "name": "SERVICE_NAME",
          "value": "my-service"
        }
      ]
    }
  ],
  "cpu": "1024",
  "memory": "2048",
  "runtimePlatform": {
    "cpuArchitecture": "X86_64",
    "operatingSystemFamily": "LINUX"
  },
  "family": "production-cluster_my-service",
  "taskRoleArn": "arn:aws:iam::123456789012:role/ecsTaskExecutionRole",
  "executionRoleArn": "arn:aws:iam::123456789012:role/ecsTaskExecutionRole",
  "networkMode": "awsvpc",
  "requiresCompatibilities": [
    "FARGATE"
  ],
  "volumes": [
    {
      "name": "shared-volume",
      "host": {}
    }
  ]
}
```
<!-- AUTO-GENERATED-TASK-DEF-END -->
