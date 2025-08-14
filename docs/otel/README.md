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
