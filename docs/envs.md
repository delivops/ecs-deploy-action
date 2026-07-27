# Environment Variables

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

Note that Python's `str()` is used, so YAML booleans become `"True"` / `"False"`, not
`"true"` / `"false"`. Quote the value if your application needs lowercase.

## Sharing Env Vars Across YAMLs (`envs_from_files`)

When several deploy YAMLs need the same block of env vars, factor them out into a `.env` file and
reference it instead of copy-pasting:

```yaml
envs_from_files:
  - ./shared/common.env
envs:
  - DATABASE_URL: "postgresql://prod-host:5432/db"  # overrides common.env

services_overrides:
  api-service:
    envs_from_files:
      - ./shared/api.env  # appended to the base list
```

### File format

Strict-minimal dotenv: `KEY=value`, blank lines, full-line `#` comments, and surrounding matched
`"…"` / `'…'` quotes stripped. Keys must match `[A-Za-z_][A-Za-z0-9_]*`.

Not supported, and a parse error rather than a silent skip: `export` prefixes, inline comments,
`${VAR}` interpolation, escape sequences, and lines without `=`.

```dotenv
# shared/common.env
LOG_LEVEL=info
DATABASE_URL="postgresql://localhost:5432/db"
```

### Precedence

Highest wins:

1. Inline `envs:` — base plus any per-service entries
2. Later files in `envs_from_files` override earlier files
3. Values from `envs_from_files`

`envs_from_files` is an array field, so per-service entries are **appended** to the base list (same
merge rule as `envs:`), which is why a service's own file wins over the base file.

### Paths

Resolved relative to the **YAML config file's** directory, not the workflow working directory.
Absolute paths are also accepted. A missing file is a hard error.

## Examples

| File | Shows |
|---|---|
| [`examples/envs-only.yaml`](../examples/envs-only.yaml) | inline env vars only |
| [`examples/envs-from-files.yaml`](../examples/envs-from-files.yaml) | dotenv files with per-service overrides |
