# Example Configurations

The `examples/` directory contains a variety of YAML configurations demonstrating all supported features. Each example is self-contained and can be used as a template for your own ECS deployments.

Every example also doubles as a test case: `tests/expected_outputs/<name>.json` holds the task definition it generates.

## Documentation Index

- [Basic Usage](basic.md) — minimal workflow, core YAML fields, `replica_count`
- [Environment Variables](envs.md) — `envs` and `envs_from_files`
- [Secrets and Secret Files](secrets.md) — all three secret formats, `secret_files`
- [Port Configuration](ports.md) — `port`, `additional_ports`, `app_protocol`
- [Health Checks](health-check.md)
- [CPU, Memory, Architecture](architecture.md)
- [Task and Execution Roles](roles.md) — `role_arn`, per-slot keys, SSM discovery
- [EC2 Launch Type](ec2-launch-type.md) — `launch_type`, `network_mode`
- [Linux Parameters](linux-parameters.md) — init process, capabilities, tmpfs, devices
- [Fluent Bit Log Collector](fluent-bit.md)
- [OpenTelemetry Collector](otel.md)

Multi-service YAML (`services_overrides`), the action inputs, and the scheduled/triggerable
deployment types are documented in the [README](../README.md).

## Example Index

| Example | Shows |
|---|---|
| [`minimal-config.yaml`](../examples/minimal-config.yaml) | the smallest working config |
| [`task.yaml`](../examples/task.yaml) | a typical service with secrets, ports, sidecars |
| [`full-example-available.yaml`](../examples/full-example-available.yaml) | every option, heavily commented |
| [`envs-only.yaml`](../examples/envs-only.yaml) | environment variables without secrets |
| [`envs-from-files.yaml`](../examples/envs-from-files.yaml) | shared env vars from dotenv files |
| [`complex-secrets.yaml`](../examples/complex-secrets.yaml) | mixed secret formats |
| [`secrets-new-format.yaml`](../examples/secrets-new-format.yaml) | grouped `secrets_envs` with `id` + `values` |
| [`secret-names.yaml`](../examples/secret-names.yaml) | name-only secrets, keys discovered at build time |
| [`secret-files-only.yaml`](../examples/secret-files-only.yaml) | secret files, no env secrets |
| [`custom-secrets-path.yaml`](../examples/custom-secrets-path.yaml) | `secrets_files_path` override |
| [`no-ports.yaml`](../examples/no-ports.yaml) | background worker with no port mappings |
| [`multiple-ports.yaml`](../examples/multiple-ports.yaml) | main plus additional named ports |
| [`health-check-variations.yaml`](../examples/health-check-variations.yaml) | health check tuning |
| [`high-resources.yaml`](../examples/high-resources.yaml) | large CPU/memory tier |
| [`x86-architecture.yaml`](../examples/x86-architecture.yaml) | explicit `cpu_arch` |
| [`ephemeral-storage.yaml`](../examples/ephemeral-storage.yaml) | `ephemeral_storage` sizing |
| [`stop-timeout.yaml`](../examples/stop-timeout.yaml) | `stop_timeout` |
| [`readonly-root-filesystem.yaml`](../examples/readonly-root-filesystem.yaml) | `readonly_root_filesystem` + `writable_dirs` |
| [`public-image.yaml`](../examples/public-image.yaml) | image from a public registry |
| [`private-dkr-image.yaml`](../examples/private-dkr-image.yaml) | image from a private registry |
| [`multi-service.yaml`](../examples/multi-service.yaml) | `services_overrides` |
| [`multi-service-roles.yaml`](../examples/multi-service-roles.yaml) | per-service role overrides |
| [`separate-roles.yaml`](../examples/separate-roles.yaml) | distinct task and execution roles |
| [`role-arn-with-override.yaml`](../examples/role-arn-with-override.yaml) | shared `role_arn`, one slot overridden |
| [`ec2-basic.yaml`](../examples/ec2-basic.yaml) | EC2 launch type, `awsvpc` |
| [`ec2-bridge-mode.yaml`](../examples/ec2-bridge-mode.yaml) | EC2 with dynamic host ports |
| [`ec2-linux-parameters.yaml`](../examples/ec2-linux-parameters.yaml) | EC2-only Linux parameters |
| [`fargate-linux-parameters.yaml`](../examples/fargate-linux-parameters.yaml) | Fargate-safe Linux parameters |
| [`otel-default-image.yaml`](../examples/otel-default-image.yaml) | default AWS OTEL collector |
| [`otel-custom-image.yaml`](../examples/otel-custom-image.yaml) | custom OTEL collector image |
| [`otel-custom-config.yaml`](../examples/otel-custom-config.yaml) | OTEL with a custom config file |
