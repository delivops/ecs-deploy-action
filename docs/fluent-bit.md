# Fluent Bit Log Collector

You can add Fluent Bit as a sidecar for advanced log routing. It is enabled only when
`fluent_bit_collector.image_name` is set to a non-empty value.

## Options

| Option | Description |
|---|---|
| `image_name` | Fluent Bit image. **Always** pulled from the ECR registry, regardless of the `ecr_registry` action input — that input only applies to the main app image. Required to enable the sidecar. |
| `extra_config` | Config file name, used as `extra/<extra_config>`. Defaults to `extra.conf`. |
| `ecs_log_metadata` | Value of `enable-ecs-log-metadata`. Defaults to `"true"`. |
| `service_name` | Value of the `SERVICE_NAME` env var. Defaults to the service name. |

## Example YAML

```yaml
fluent_bit_collector:
  image_name: fluent-bit:2.1.0
  extra_config: custom-fluent-bit.conf
  ecs_log_metadata: "true"
  service_name: "my-custom-service"  # optional, defaults to the app name
```

## Effect on the app container

Enabling Fluent Bit changes how the main container logs:

- Its `logConfiguration` switches from `awslogs` to `awsfirelens`.
- It gains a `dependsOn` entry requiring the `fluent-bit` container to reach `START`.

The sidecar itself keeps logging to `awslogs` under the `fluentbit` stream prefix, is marked
`essential: true` (if it fails, the task fails), and has a health check against
`http://127.0.0.1:2020/api/v1/health`.

Without a `fluent_bit_collector` block, the app container logs directly to CloudWatch Logs via
`awslogs` in the log group `/ecs/<cluster>/<service>`.
