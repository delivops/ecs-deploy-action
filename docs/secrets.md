# Secrets and Secret Files

You can inject secrets from AWS Secrets Manager as environment variables or download secret files at runtime. Supported formats:

- `secrets`: Classic format, map env var to a Secrets Manager secret ARN or an SSM parameter ARN
- `secrets_envs`: Grouped format, reference multiple keys from one secret
- `secrets_envs` (name-only): Auto-extract all keys from a secret as environment variables
- `secrets_envs` with `env`: the whole secret as one environment variable (single-value secrets)
- `secret_files`: List of secret files to download to `/etc/secrets/`

See the [full YAML example](../README.md#complete-yaml-configuration-example) for usage.

# Secrets and Secret Files Example

You can inject secrets from AWS Secrets Manager as environment variables or download secret files at runtime.

## Classic Format

```yaml
secrets:
  - DATABASE_PASSWORD: arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-db-password
  - API_KEY: arn:aws:secretsmanager:us-east-1:123456789012:secret:external-api-key
```

## SSM Parameter Store

A `secrets` entry whose value is an SSM parameter ARN is passed through unchanged as `valueFrom`
(the parameter holds the value itself, so there is no JSON key to append). ECS task definitions
and AWS Batch job definitions both accept it, and it can be mixed with Secrets Manager entries:

```yaml
secrets:
  - DATABASE_PASSWORD: arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-db-password
  - API_TOKEN: arn:aws:ssm:us-east-1:123456789012:parameter/my-app/api-token
```

renders as

```json
"secrets": [
  {"name": "DATABASE_PASSWORD", "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-db-password:DATABASE_PASSWORD::"},
  {"name": "API_TOKEN", "valueFrom": "arn:aws:ssm:us-east-1:123456789012:parameter/my-app/api-token"}
]
```

Use the full ARN (`arn:aws:ssm:<region>:<account-id>:parameter/<path>`); a `SecureString` works
the same way. The execution role needs `ssm:GetParameters` on the parameter, and `kms:Decrypt` if
it is encrypted with a customer-managed key. See
[examples/secrets-ssm-parameter.yaml](../examples/secrets-ssm-parameter.yaml).

## Grouped Format

```yaml
secrets_envs:
  - id: arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123
    values:
      - DATABASE_PASSWORD
      - API_KEY
```

## Whole Secret as One Variable (`env`)

For a secret that holds a single value rather than JSON — an API key, a token, a licence string —
give the `secrets_envs` entry an `env` instead of `values`:

```yaml
secrets_envs:
  - id: arn:aws:secretsmanager:us-east-1:123456789012:secret:stripe-api-key-AbCdEf
    env: STRIPE_API_KEY
  - name: license-key            # resolved to the full ARN at deploy time
    env: LICENSE_KEY
```

renders as

```json
"secrets": [
  {"name": "STRIPE_API_KEY", "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:stripe-api-key-AbCdEf"},
  {"name": "LICENSE_KEY", "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:license-key-XyZ123"}
]
```

The reference is the secret's ARN with no JSON-key suffix, so the container receives the entire
secret string. Used on a JSON secret, the variable gets the whole JSON document.

- `id` is used exactly as given. Give the **full** ARN, including the 6-character suffix Secrets
  Manager adds to the name; a partial ARN is ambiguous to AWS.
- `name` is resolved to the full ARN with `secretsmanager:DescribeSecret` — metadata only, the
  value is never read — so the deploy role needs that permission on the secret. A name that
  doesn't exist, or that the deploy role can't describe, fails the deploy.
- `env` and `values` can't be combined; an `env` name that is already set by `envs` or another
  `secrets_envs` entry fails the deploy.
- `secrets_envs` is ignored whenever `secrets` is also set (long-standing behaviour); an `env`
  entry in that situation fails the deploy instead of being dropped.
- Works for the application container, sidecars, `services_overrides` and
  `deployment_type: batch_job_definition`. As with every secret, the execution role needs
  `secretsmanager:GetSecretValue` on it.

See [examples/secrets-envs-whole-secret.yaml](../examples/secrets-envs-whole-secret.yaml).

## Secret Names Format (Auto-Discover Keys at Build Time)

This format automatically discovers all keys from the specified secrets at build time and generates the traditional ECS secrets format:

```yaml
secrets_envs:
  - name: database-credentials  
  - name: oauth-config
```

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
