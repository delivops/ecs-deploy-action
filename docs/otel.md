# OpenTelemetry Collector Support

This project supports adding an OpenTelemetry collector as a sidecar to your ECS service. You can use either the default AWS OTEL collector or a custom image. Configuration options include:

- `otel_collector.image_name`: Custom image name (uses ECR registry)
- `otel_collector.extra_config`: Path to custom config file
- `otel_collector.ssm_name`: SSM parameter for config (default AWS image)
- `otel_collector.metrics_port`: Metrics port (default: 8080)
- `otel_collector.metrics_path`: Metrics path (default: /metrics)

See the [full YAML example](../README.md#complete-yaml-configuration-example) for usage.

# OpenTelemetry Collector Example

Add an OpenTelemetry collector as a sidecar for observability.

## Example YAML

```yaml
otel_collector:
  image_name: custom-otel-collector
  extra_config: otel-config.yaml
  metrics_port: 8080
  metrics_path: /metrics
```
