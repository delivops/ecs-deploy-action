![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)

# ECS Deploy Action

This GitHub Action deploys applications to Amazon ECS using a simple YAML configuration. The documentation below is fully dynamic and always up-to-date.

This GitHub Action deploys applications to Amazon ECS using a simple YAML configuration.

**All full examples, advanced features, and explanations are in the [`docs/`](docs/) directory.**

Below you’ll find the dynamic, always up-to-date configuration and generated ECS task definition.
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

For feature explanations, example walkthroughs, and advanced usage, see the [docs/](docs/) directory.
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
          "name": "ENABLE_METRICS",
          "value": "True"
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
          "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-password:DATABASE_PASSWORD::"
        },
        {
          "name": "API_KEY",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:api-key:API_KEY::"
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
          "value": "/metrics"
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
