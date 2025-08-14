# Basic Usage

This is the minimal configuration for deploying an application to ECS using this action.

## Example Workflow

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

## Basic YAML Configuration

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
command: ["npm", "start"]
entrypoint: ["/usr/local/bin/docker-entrypoint.sh"]
health_check:
  command: "curl -f http://localhost:8080/health || exit 1"
  interval: 30
  timeout: 5
  retries: 3
  start_period: 60
```
