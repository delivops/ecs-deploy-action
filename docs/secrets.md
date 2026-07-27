# Secrets and Secret Files

You can inject secrets from AWS Secrets Manager as environment variables or download secret files at runtime. Supported formats:

- `secrets`: Classic format, map env var to secret ARN
- `secrets_envs`: Grouped format, reference multiple keys from one secret
- `secrets_envs` (name-only): Auto-extract all keys from a secret as environment variables
- `secret_files`: List of secret files to download to `/etc/secrets/`

> **`secrets` and `secrets_envs` are mutually exclusive.** If `secrets` is present and non-empty it
> is used and `secrets_envs` is **ignored entirely**, with no warning. Pick one format per config —
> and remember that under `services_overrides` both keys are appended to the base list, so a base
> `secrets` block will shadow a per-service `secrets_envs` block.

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

## Secret Names Format (Auto-Discover Keys at Build Time)

This format automatically discovers all keys from the specified secrets at build time and generates the traditional ECS secrets format:

```yaml
secrets_envs:
  - name: database-credentials  
  - name: oauth-config
```

> **This format calls AWS at task-definition generation time**, so the deploy step needs
> `secretsmanager:GetSecretValue` on each named secret. If the call fails — no credentials, expired
> token, secret not found — the generator logs a warning and falls back to **placeholder keys and a
> placeholder ARN** so local and offline runs still produce output. That means a credentials problem
> in CI yields a task definition wired to secrets that do not exist rather than a failed deploy.
> Prefer the `id` + `values` format when you want the config to be fully deterministic and offline.

### How it works:

1. **Build-time Discovery**: The script queries AWS Secrets Manager during task definition generation
2. **Automatic Key Detection**: All JSON keys in each secret are discovered automatically
3. **Traditional Format Generation**: Creates standard ECS `valueFrom` entries for each discovered key
4. **No Runtime Overhead**: No init containers or runtime processing needed

### Example:

If your AWS secret `database-credentials` contains:
```json
{
  "DB_HOST": "prod-db.example.com",
  "DB_PORT": "5432",
  "DB_USER": "app_user", 
  "DB_PASSWORD": "secure_password"
}
```

The generated task definition will contain:
```json
"secrets": [
  {
    "name": "DB_HOST",
    "valueFrom": "database-credentials:DB_HOST::"
  },
  {
    "name": "DB_PORT",
    "valueFrom": "database-credentials:DB_PORT::"
  },
  {
    "name": "DB_USER", 
    "valueFrom": "database-credentials:DB_USER::"
  },
  {
    "name": "DB_PASSWORD",
    "valueFrom": "database-credentials:DB_PASSWORD::"
  }
]
```

### Benefits:

- **Build-time Discovery**: Keys are discovered when the task definition is generated
- **Standard ECS Format**: Uses native ECS secrets injection (no custom containers)
- **Better Performance**: No runtime overhead or init containers needed
- **AWS Best Practices**: Follows standard AWS ECS secrets management patterns

## Secret Files

```yaml
secret_files:
  - ssl-certificate
  - private-key
  - config-file
```

By default, secret files are downloaded to `/etc/secrets/` inside the container. You can customize this path using the `secrets_files_path` option:

```yaml
secret_files:
  - ssl-certificate  
  - private-key
  - config-file
secrets_files_path: "/app/secrets"  # Custom path instead of /etc/secrets
```

This will:
- Download secrets to `/app/secrets/` instead of `/etc/secrets/`
- Mount the shared volume at `/app/secrets/` for both init and main containers
- Update all file paths in the init container commands accordingly

The `secrets_files_path` setting affects both where files are written by the init container and where they're mounted in your application container.
