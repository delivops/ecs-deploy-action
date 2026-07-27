# OpenTelemetry Collector

You can add an OpenTelemetry collector as a sidecar to your ECS task. Adding an `otel_collector`
block to the YAML — even an empty one — adds the container.

## Options

| Option | Description |
|---|---|
| `image_name` | Custom collector image. **Always** pulled from the ECR registry, regardless of the `ecr_registry` action input — that input only applies to the main app image. Omit to use the default AWS collector. |
| `extra_config` | Config file name inside the image, used as `--config /conf/<extra_config>`. Custom images only. |
| `ssm_name` | SSM parameter holding the config, injected as the `SSM_CONFIG` secret. Default AWS image only. Defaults to `adot-config-global.yaml`. |
| `metrics_port` | Value of the `METRICS_PORT` env var. Default `8080`. |
| `metrics_path` | Value of the `METRICS_PATH` env var. Default `/metrics`. |

## Default AWS image

With no `image_name`, the container runs
`public.ecr.aws/aws-observability/aws-otel-collector:latest` with `--config env:SSM_CONFIG`, and
the config is read from the SSM parameter named by `ssm_name`:

```yaml
otel_collector:
  ssm_name: production-otel-config.yaml
```

## Custom image

A custom image is read from `--config /conf/<extra_config>`, or `/conf/config.yaml` when
`extra_config` is unset. Custom images also get a `SERVICE_NAME` env var set to the service name,
and no `SSM_CONFIG` secret:

```yaml
otel_collector:
  image_name: custom-otel-collector:v1.0.0
  extra_config: otel-config.yaml
  metrics_port: 8080
  metrics_path: /metrics
```

## Generated container

Either way the sidecar is marked `essential: true` — if the collector fails, the task fails — and
exposes both OTLP ports: 4317 (gRPC) and 4318 (HTTP).

## Examples

| File | Shows |
|---|---|
| [`examples/otel-default-image.yaml`](../examples/otel-default-image.yaml) | default AWS collector with SSM config |
| [`examples/otel-custom-image.yaml`](../examples/otel-custom-image.yaml) | custom collector image |
| [`examples/otel-custom-config.yaml`](../examples/otel-custom-config.yaml) | custom config file path |
