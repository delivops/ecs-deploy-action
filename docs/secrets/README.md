# Secrets and Secret Files Example

You can inject secrets from AWS Secrets Manager as environment variables or download secret files at runtime.

## Classic Format

```yaml
secrets:
  - DATABASE_PASSWORD: arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-db-password
  - API_KEY: arn:aws:secretsmanager:us-east-1:123456789012:secret:external-api-key
```

## Grouped Format

```yaml
secrets_envs:
  - id: arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123
    values:
      - DATABASE_PASSWORD
      - API_KEY
```

## Secret Files

```yaml
secret_files:
  - ssl-certificate
  - private-key
  - config-file
```
