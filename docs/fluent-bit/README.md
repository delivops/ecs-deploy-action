# Fluent Bit Log Collector Example

Add Fluent Bit as a sidecar for advanced log routing.

## Example YAML

```yaml
fluent_bit_collector:
  image_name: fluent-bit:2.1.0
  extra_config: custom-fluent-bit.conf
  ecs_log_metadata: 'true'
```
