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
        uses: delivops/ecs-deploy-action@v1
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
stop_timeout: 30  # optional, seconds to wait before forcefully killing container
health_check:
  command: "curl -f http://localhost:8080/health || exit 1"
  interval: 30
  timeout: 5
  retries: 3
  start_period: 60
```

> **`replica_count` sets the service's desired count on every deploy.** Omit it for services under
> autoscaling, otherwise each deploy resets the running count to this value. Values that are not
> non-negative integers are ignored with a warning rather than failing the deploy.

## Other Container Options

| Option | Default | Description |
|---|---|---|
| `stop_timeout` | unset | Seconds to wait for the container to exit before it is killed. |
| `ephemeral_storage` | unset (AWS default 20) | Task ephemeral storage in GiB. |
| `readonly_root_filesystem` | unset | Mounts the root filesystem read-only on **all** containers in the task, including sidecars. |
| `writable_dirs` | `[]` | Paths to mount as empty writable volumes on **all** containers. Needed when `readonly_root_filesystem: true`. |
| `secrets_files_path` | `/etc/secrets` | Where `secret_files` are written and mounted. See [Secrets](secrets.md). |
| `app_protocol` | `http` | `appProtocol` for all port mappings. See [Ports](ports.md). |
| `launch_type` / `network_mode` | `FARGATE` / `awsvpc` | See [EC2 Launch Type](ec2-launch-type.md). |
| `linux_parameters` | unset | See [Linux Parameters](linux-parameters.md). |

## Other Deployment Types

This page covers `deployment_type: service`. For EventBridge-scheduled tasks
(`scheduled_task`) and standalone task definitions (`triggerable_task`), see the
[README](../README.md#deployment-types). Those types are not managed by
`terraform-aws-ecs-service`, so their role ARNs must be set in YAML — see
[Task and Execution Roles](roles.md#scheduled-and-triggerable-tasks).
