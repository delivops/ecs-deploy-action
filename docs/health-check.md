# Health Checks

You can configure a container health check for the main application container.

## Example YAML

```yaml
health_check:
  command: "curl -f http://localhost:8080/health || exit 1"
  interval: 30 # seconds between health checks
  timeout: 5 # seconds to wait for health check
  retries: 3 # number of consecutive failures before unhealthy
  start_period: 60 # seconds to wait before first health check
```

## Fields

| Field | Default | Description |
|---|---|---|
| `command` | — | Shell command. Required; wrapped as `["CMD-SHELL", <command>]`. |
| `interval` | 30 | Seconds between checks. |
| `timeout` | 5 | Seconds to wait for a check to finish. |
| `retries` | 3 | Consecutive failures before the container is unhealthy. |
| `start_period` | 10 | Grace period in seconds before failures start counting. |

The health check is only added when `health_check.command` is present and non-empty. A
`health_check` block without a `command` is silently skipped, so no `healthCheck` reaches the task
definition — check for a typo there if yours seems to be ignored.

The command runs inside your container, so whatever it invokes (`curl`, `wget`, …) must be present
in the image.

See [`examples/health-check-variations.yaml`](../examples/health-check-variations.yaml).

## Sidecar health checks

The Fluent Bit sidecar has its own fixed health check and is not affected by this block. See
[Fluent Bit](fluent-bit.md).
