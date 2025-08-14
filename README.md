![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)

# ECS Deploy Action

This GitHub Action deploys applications to Amazon ECS using a simple YAML configuration.

**All full examples, advanced features, and explanations are in the [`docs/`](docs/) directory.**

Below you’ll find the dynamic, always up-to-date configuration and generated ECS task definition.
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
![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)

# ECS Deploy Action

This GitHub Action deploys applications to Amazon ECS using a simple YAML configuration.

**All full examples, advanced features, and explanations are in the [`docs/`](docs/) directory.**

Below you’ll find the dynamic, always up-to-date configuration and generated ECS task definition.

<!-- AUTO-GENERATED-YAML-START -->
...existing code...
<!-- AUTO-GENERATED-YAML-END -->

<!-- AUTO-GENERATED-TASK-DEF-START -->
...existing code...
<!-- AUTO-GENERATED-TASK-DEF-END -->
      "environment": [
