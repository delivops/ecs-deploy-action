# Secrets and Secret Files

You can inject secrets from AWS Secrets Manager as environment variables or download secret files at runtime. Supported formats:

- `secrets`: Classic format, map env var to secret ARN
- `secrets_envs`: Grouped format, reference multiple keys from one secret
- `secret_files`: List of secret files to download to `/etc/secrets/`

See the [full YAML example](../README.md#complete-yaml-configuration-example) for usage.
