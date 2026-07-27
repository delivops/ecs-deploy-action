# EC2 Launch Type

This action supports both **Fargate** (default) and **EC2** launch types for ECS task definitions.

## Configuration

```yaml
# EC2 launch type with awsvpc network mode
launch_type: EC2
network_mode: awsvpc  # default

# EC2 with bridge network mode (enables dynamic host ports)
launch_type: EC2
network_mode: bridge
```

## Launch Type Options

| Value | Description |
|-------|-------------|
| `FARGATE` | (Default) Serverless container execution |
| `EC2` | Container instances managed by you |

## Network Mode Options

| Value | Fargate | EC2 | Description |
|-------|---------|-----|-------------|
| `awsvpc` | ✅ Required | ✅ Supported | Each task gets its own ENI |
| `bridge` | ❌ | ✅ Supported | Docker's built-in virtual network |
| `host` | ❌ | ✅ Supported | Container uses host's network directly |
| `none` | ❌ | ✅ Supported | No external network connectivity |

## Differences Between Launch Types

### CPU/Memory Validation

- **Fargate**: Strict validation with fixed CPU values (256, 512, 1024, 2048, 4096) and matching memory tiers
- **EC2**: Flexible - any positive integer, or omit for container-level allocation

### Runtime Platform

- **Fargate**: `runtimePlatform` block is automatically included (required)
- **EC2**: `runtimePlatform` block is not included

### Port Mappings

- **awsvpc/host modes**: `hostPort` equals `containerPort`
- **bridge mode**: `hostPort` is set to `0` for dynamic port assignment

## Examples

### Basic EC2 Configuration

```yaml
name: my-ec2-app
cpu: 512
memory: 1024
launch_type: EC2
network_mode: awsvpc
port: 8080
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
```

### EC2 with Bridge Network Mode

```yaml
name: my-bridge-app
cpu: 1024
memory: 2048
launch_type: EC2
network_mode: bridge
port: 8080
additional_ports:
  - metrics: 9090
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
```

This will generate port mappings with dynamic host ports:
```json
{
  "portMappings": [
    {
      "containerPort": 8080,
      "hostPort": 0,
      "protocol": "tcp"
    }
  ]
}
```

## See Also

- [Linux Parameters](linux-parameters.md) - Configure Linux-specific container settings
- [Basic Configuration](basic.md) - Core configuration options
