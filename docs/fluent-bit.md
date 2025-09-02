# Fluent Bit Log Collector Support

You can add Fluent Bit as a sidecar for advanced log routing in your ECS service. Configuration options include:

- `fluent_bit_collector.image_name`: Fluent Bit image (always uses ECR registry)
- `fluent_bit_collector.extra_config`: Path to custom Fluent Bit config file
- `fluent_bit_collector.ecs_log_metadata`: Enable ECS log metadata
- `fluent_bit_collector.service_name`: Custom service name for SERVICE_NAME environment variable (defaults to app name)

See the [full YAML example](../README.md#complete-yaml-configuration-example) for usage.

# Fluent Bit Log Collector Example

Add Fluent Bit as a sidecar for advanced log routing.

## Example YAML

```yaml
fluent_bit_collector:
  image_name: fluent-bit:2.1.0
  extra_config: custom-fluent-bit.conf
  ecs_log_metadata: "true"
  service_name: "my-custom-service"  # Optional: defaults to app name
```
