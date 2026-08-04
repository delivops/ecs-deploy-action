# Sidecars

A `sidecars:` list adds extra containers to the generated task definition. They are ordinary ECS
containers with their own image, environment, secrets, mounts, ports and health check — useful for
a metrics agent, a cache, a log tailer, or a small writable container so the ECS Exec managed agent
has somewhere to run while the application keeps a read-only root filesystem.

Everything ends up in the **single** `task-definition.json` this action already registers. There is
no second registration and no second deployment.

> This is separate from `fluent_bit_collector` and `otel_collector`, which remain dedicated blocks
> with their own defaults. Those are unchanged.

## Minimal example

```yaml
cpu: 256
memory: 512
port: 8080

sidecars:
  - name: metrics-proxy
    image: public.ecr.aws/nginx/nginx:1.27
```

That is enough. The container is `essential: true` (the ECS default), has no environment, no
secrets, no mounts and no ports, and logs to the service's CloudWatch log group under the stream
prefix `metrics-proxy`.

## Isolation: a sidecar gets only what it declares

This is the rule that matters most. A sidecar **never** inherits the application container's
`envs`, `envs_from_files`, `secrets`, `secrets_envs`, `secret_files`, `writable_dirs`, `port`,
`additional_ports`, `health_check` or `linux_parameters`. If a sidecar needs an environment
variable or a secret, declare it inside that sidecar's own block.

The single exception is `readonly_root_filesystem`, which falls back to the top-level value when
the sidecar does not state one of its own — see [Read-only root filesystems](#read-only-root-filesystems).

## Schema

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | **required** | ECS container name. Must be unique — see [Reserved names](#reserved-and-generated-names). |
| `image` | string | **required** | Full image reference. Pin sidecars by digest: nothing in this pipeline rebuilds them. |
| `enabled` | bool | `true` | `false` removes the container. Mainly for per-service overrides. |
| `essential` | bool | `true` | `false` lets the container exit without failing the task. |
| `command` | list | `[]` | → `command` |
| `entrypoint` | list | `[]` | → `entryPoint` |
| `envs` | list of single-key maps | `[]` | → `environment`. Values are stringified, as for the app. |
| `envs_from_files` | list of paths | `[]` | dotenv files, resolved relative to the task config YAML. Inline `envs` win. |
| `secrets` | list of single-key maps | `[]` | Legacy format → `secrets[].valueFrom`. |
| `secrets_envs` | list | `[]` | Grouped format. Ignored when a non-empty `secrets` is also set — same rule as the app. |
| `secret_files` | list of secret names | `[]` | Generates a private init container and volume. See [Secret files](#secret-files). |
| `secrets_files_path` | string | `/etc/secrets` | Where this sidecar's secret files are mounted. |
| `writable_dirs` | list of paths | `[]` | Generates an empty volume per path and mounts it. |
| `health_check` | map | — | `command`, `interval`, `timeout`, `retries`, `start_period`. Same shape as the app's. |
| `linux_parameters` | map | — | Same shape and support matrix as [linux-parameters.md](./linux-parameters.md). |
| `port` | int | — | → a `portMappings` entry named `<name>-<port>-tcp`. |
| `additional_ports` | list of single-key maps | `[]` | → extra `portMappings`; the key is the mapping name. |
| `app_protocol` | string | `http` | `http`, `grpc` or `tcp`. `tcp` omits `appProtocol`. |
| `cpu` | int | — | Container-level CPU units. |
| `memory` | int | — | Container-level hard memory limit, MiB. |
| `memory_reservation` | int | — | Soft limit, MiB. Must not exceed `memory`. |
| `readonly_root_filesystem` | bool | inherits | See below. |
| `stop_timeout` | int | — | Seconds before SIGKILL. |
| `log_stream_prefix` | string | the sidecar's `name` | CloudWatch stream prefix. |

Anything else is rejected. A misspelled key fails the deploy with the offending key named, rather
than being silently ignored — a sidecar quietly missing the secret its author thought they had
configured is far worse than a failed build.

### Logging

Sidecars always use the `awslogs` driver, writing to the service log group
`/ecs/<cluster>/<service>` with the sidecar's name as the stream prefix. They do **not** follow the
application onto `awsfirelens` when `fluent_bit_collector` is enabled — the existing `fluent-bit`
and `otel-collector` containers behave the same way. This is deliberate: routing a sidecar's logs
through fluent-bit would make the sidecar's own diagnostics unavailable exactly when fluent-bit is
the thing that is broken.

## Read-only root filesystems

`readonly_root_filesystem` is resolved per container:

1. the sidecar's own value, if it sets one;
2. otherwise the top-level `readonly_root_filesystem`;
3. otherwise the key is omitted entirely.

The application container, its secret-file init container, `fluent-bit` and `otel-collector`
continue to take the top-level value exactly as before.

> ⚠️ **Inheriting read-only does not inherit the writable mounts that make it survivable.**
> A sidecar inherits `readonly_root_filesystem: true` but — by the isolation rule — receives none
> of the application's `writable_dirs`. A container that needs scratch space must declare its own:
>
> ```yaml
> readonly_root_filesystem: true
> writable_dirs: [/tmp]          # the application's, and only the application's
>
> sidecars:
>   - name: cache
>     image: public.ecr.aws/docker/library/redis:7-alpine
>     writable_dirs: [/tmp]      # required: the sidecar is read-only too
> ```
>
> Without that second `writable_dirs`, redis starts read-only with nowhere to write. Either declare
> the directories the sidecar needs, or set `readonly_root_filesystem: false` on it.

### Read-only application + writable ECS Exec bridge

The case this feature was built for. The application stays read-only with a writable `/tmp`, while
a tiny bridge container runs writable so the ECS Exec managed agent can unpack itself:

```yaml
readonly_root_filesystem: true
writable_dirs:
  - /tmp

port: 8080
envs:
  - NODE_ENV: production
command: ["npm", "start"]

sidecars:
  - name: ssm-bridge
    image: public.ecr.aws/amazonlinux/amazonlinux@sha256:<pinned-digest>
    essential: true
    command:
      - sleep
      - infinity
    readonly_root_filesystem: false
    linux_parameters:
      init_process_enabled: true
```

The bridge gets `readonlyRootFilesystem: false`, and no application environment, secrets,
secret-file mounts, writable directories or port mappings. Full example:
[`examples/sidecar-readonly-bridge.yaml`](../examples/sidecar-readonly-bridge.yaml).

## Environment and secrets

Secrets remain ECS `valueFrom` references. They are never resolved into plaintext values in the
task definition.

```yaml
sidecars:
  - name: metrics
    image: public.ecr.aws/my-org/metrics-agent:v1.4.2
    envs:
      - AGENT_MODE: push
      - SCRAPE_INTERVAL: 15
    envs_from_files:
      - ./shared/common.env
    secrets_envs:
      - id: arn:aws:secretsmanager:us-east-1:123456789012:secret:metrics-abc123
        values:
          - REMOTE_WRITE_USER
          - REMOTE_WRITE_PASSWORD
    port: 9090
    cpu: 128
    memory: 256
```

`envs_from_files` paths resolve relative to the task config YAML, the same as at the top level, and
only ever populate the sidecar that declares them. Full example:
[`examples/sidecar-full-features.yaml`](../examples/sidecar-full-features.yaml).

## Secret files

A sidecar with `secret_files` gets its **own** init container and its **own** volume — it does not
share the application's. The sidecar depends on that init container with condition `SUCCESS`, so it
only starts once its files are on disk.

```yaml
sidecars:
  - name: metrics
    image: public.ecr.aws/my-org/metrics-agent:v1.4.2
    secret_files:
      - metrics-client-cert
    secrets_files_path: /etc/metrics-secrets
```

## Reserved and generated names

Generated names are prefixed with the sidecar's name, so two sidecars that both want a writable
`/tmp` get two separate volumes rather than accidentally sharing one scratch directory:

| Thing | Application | Sidecar `metrics` |
|---|---|---|
| secret-file init container | `init-container-for-secret-files` | `metrics-secret-init` |
| secret-file volume | `shared-volume` | `metrics-secrets` |
| writable `/tmp` volume | `writable-tmp` | `metrics-writable-tmp` |
| primary port mapping | `default` | `metrics-9090-tcp` |

A sidecar name may not be `app`, `fluent-bit`, `otel-collector`, `init-container-for-secret-files`
or `default`, may not duplicate another sidecar, and may not collide with a name generated for
another sidecar. Generated volume names must be valid ECS names (letters, digits, `-` and `_`), so
a directory path containing other characters is rejected up front rather than at
`RegisterTaskDefinition` time.

Both port namespaces are validated too, because ECS scopes them to the whole task rather than to a
single container:

- **Port mapping names** must be unique task-wide, which is why a sidecar's primary port is not
  called `default`. ECS also requires them to be lowercase, so a sidecar named `My_Proxy` with a
  `port` is rejected here rather than by AWS.
- **Container ports** must be unique task-wide under `awsvpc` and `host`, where every container
  shares one network interface — an application on `8080` and a sidecar on `8080` cannot coexist.
  Under `bridge` the host port is assigned dynamically, so duplicates are allowed.

## Per-service overrides

Under `services_overrides`, sidecars merge **by name** rather than being appended — otherwise a
service override would produce two containers with the same name, which ECS rejects.

For a matched name, the usual override rules apply inside the sidecar: scalars replace, arrays
extend, maps replace wholesale, and an explicit `null` deletes the key. A name not present in the
base list is appended.

```yaml
sidecars:
  - name: ssm-bridge
    image: public.ecr.aws/amazonlinux/amazonlinux:2023
    command: ["sleep", "infinity"]
    readonly_root_filesystem: false

  - name: cache
    image: public.ecr.aws/docker/library/redis:7-alpine
    memory_reservation: 64
    envs:
      - REDIS_MAXMEMORY: 32mb

services_overrides:
  worker-service:
    sidecars:
      # Switched off for this service only.
      - name: ssm-bridge
        enabled: false

      # Patched: memory_reservation replaced, REDIS_APPENDONLY appended to envs.
      - name: cache
        memory_reservation: 128
        envs:
          - REDIS_APPENDONLY: "no"

      # Not in the base list, so appended for this service only.
      - name: audit-tailer
        image: public.ecr.aws/my-org/audit-tailer:v1
        essential: false
```

`sidecars: null` in a service override removes every sidecar for that service.

> ⚠️ **`command` and `entrypoint` extend, they do not replace.** They are array fields, so the same
> rule that usefully appends to `envs` also appends to them — a base `command: ["sleep", "infinity"]`
> plus an override `command: ["/bin/agent"]` yields `["sleep", "infinity", "/bin/agent"]`. This
> matches how the top-level `command` has always merged. To give a service a genuinely different
> command, declare it as a separate sidecar (disable the base one with `enabled: false` and add
> yours under a new name) rather than trying to override the array in place.

A disabled sidecar is still validated — it is a base-config entry that will be switched back on one
day — but it contributes no containers, volumes or names to the task definition.

Full example:
[`examples/sidecar-service-overrides.yaml`](../examples/sidecar-service-overrides.yaml).

## Examples

| File | Shows |
|---|---|
| [`sidecar-minimal.yaml`](../examples/sidecar-minimal.yaml) | The smallest possible sidecar |
| [`sidecar-readonly-bridge.yaml`](../examples/sidecar-readonly-bridge.yaml) | Read-only app + writable ECS Exec bridge |
| [`sidecar-full-features.yaml`](../examples/sidecar-full-features.yaml) | Every supported key on one sidecar |
| [`sidecar-two-writable-tmp.yaml`](../examples/sidecar-two-writable-tmp.yaml) | Two sidecars sharing a path, separate volumes |
| [`sidecar-service-overrides.yaml`](../examples/sidecar-service-overrides.yaml) | Per-service patch, disable and add |
