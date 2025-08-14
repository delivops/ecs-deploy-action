# Environment Variables Example

You can pass environment variables to your container using the `envs` field. All values are converted to strings automatically.

## Example YAML

```yaml
envs:
  - NODE_ENV: production
  - API_VERSION: v1
  - LOG_LEVEL: info
  - MAX_CONNECTIONS: 100 # Integer - will be converted to "100"
  - ENABLE_METRICS: true # Boolean - will be converted to "True"
```
