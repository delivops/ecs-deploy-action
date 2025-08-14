# Health Check Example

You can configure custom health checks for your container.

## Example YAML

```yaml
health_check:
  command: "curl -f http://localhost:8080/health || exit 1"
  interval: 30 # seconds between health checks
  timeout: 5 # seconds to wait for health check
  retries: 3 # number of consecutive failures before unhealthy
  start_period: 60 # seconds to wait before first health check
```
