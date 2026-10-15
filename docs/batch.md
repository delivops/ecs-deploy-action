# AWS Batch Job Definitions

`deployment_type: batch_job_definition` registers a new revision of an AWS Batch job definition
from the same `task_config_yaml` format the ECS deployment types use. The image is built from
`image_name` + `tag` exactly as for ECS, and `task_name` is the job definition name.

It is opt-in: nothing changes for `service`, `scheduled_task`, `triggerable_task` or template mode.

The action only registers the job definition. The compute environment, job queue, log group and
IAM roles stay in Terraform, and whatever submits the jobs (a schedule, Step Functions, another
workflow) references the job definition by name, which always resolves to its latest ACTIVE
revision.

## Usage

```yaml
- name: Register Batch job definition
  id: batch
  uses: delivops/ecs-deploy-action@v1
  with:
    environment: production
    deployment_type: batch_job_definition
    task_name: nightly-report          # the job definition name
    ecs_cluster: production            # optional, see below
    image_name: nightly-report
    tag: ${{ github.sha }}
    task_config_yaml: config/batch.yaml
    aws_account_id: ${{ secrets.AWS_ACCOUNT_ID }}
    aws_region: us-east-1

- run: echo "Registered ${{ steps.batch.outputs.job_definition_arn }}"
```

| Input | For `batch_job_definition` |
|---|---|
| `task_name` | **Required.** The job definition name: up to 128 letters, digits, `-` and `_`. |
| `task_config_yaml` | **Required.** Only `task_definition_source: yaml` is supported. |
| `ecs_cluster` | Optional. When set, enables the SSM role fallback and the `/ecs/<cluster>/<task_name>` log group (below). |
| `dry_run` | `true` writes and prints `job-definition.json` and registers nothing. |

Output: `job_definition_arn` — the ARN of the new revision (empty on a dry run).

## YAML mapping

Existing keys are reused where they mean the same thing:

| YAML key | Job definition | Notes |
|---|---|---|
| `cpu` | `resourceRequirements` `VCPU` | **In vCPUs, not ECS CPU units.** Fractional values (`0.25`, `0.5`) on Fargate only. Default `1`. |
| `memory` | `resourceRequirements` `MEMORY` | MiB. Default `2048`. On Fargate it must be a valid combination with `cpu` (same table as ECS Fargate). |
| `command` | `containerProperties.command` | Omitted when empty, so the image's `CMD` applies. |
| `envs`, `envs_from_files` | `containerProperties.environment` | Same rules as ECS. |
| `secrets`, `secrets_envs` (both formats) | `containerProperties.secrets` | All three formats, including SSM parameter ARNs — see [secrets.md](secrets.md). |
| `task_role_arn` / `execution_role_arn` / `role_arn` | `jobRoleArn` / `executionRoleArn` | Same order as ECS — see [Roles](#roles). |
| `cpu_arch` | `runtimePlatform.cpuArchitecture` | Fargate only (`X86_64` default, or `ARM64`). On EC2 the compute environment decides. |
| `ephemeral_storage` | `ephemeralStorage.sizeInGiB` | Fargate only, 21–200. |
| `services_overrides.<task_name>` | — | Applied with `task_name` as the key, exactly like a service name. |

Batch-only keys:

| YAML key | Job definition | Default |
|---|---|---|
| `platform` | `platformCapabilities` | `EC2` (or `FARGATE`) |
| `timeout_seconds` | `timeout.attemptDurationSeconds` | none; at least 60 |
| `retry_attempts` | `retryStrategy.attempts` | `1`; 1–10 |
| `parameters` | `parameters` (defaults for `Ref::<name>`) | none |
| `propagate_tags` | `propagateTags` | `true` |
| `deregister_previous` | deregisters older ACTIVE revisions after registering | `false` |

### `cpu` is in vCPUs

ECS task configs express `cpu` in CPU units (`1024` = 1 vCPU). Batch takes vCPUs. A value that
looks like CPU units (a multiple of 256, such as `256` or `1024`) is rejected with a hint, so a YAML
shared with a service can't accidentally ask for 1024 vCPUs. Override it for the job instead:

```yaml
cpu: 512          # the service: ECS CPU units
memory: 1024
services_overrides:
  nightly-report: # the job (task_name)
    platform: FARGATE
    cpu: 0.5      # vCPUs
    memory: 2048
```

### Parameters

`parameters` sets default values that a job submission can override. Batch substitutes
`Ref::<name>` **only when it is a whole element of `command`** — `"Ref::date"` works,
`"--date=Ref::date"` is passed through literally. Split such arguments into two elements:

```yaml
command: ["python", "-m", "worker", "--date", "Ref::date"]
parameters:
  date: "today"
```

### `deregister_previous`

Off by default. A job that is running or queued keeps the revision it was submitted with either
way, but older revisions stay available for re-running or rolling back. With `true`, every ACTIVE
revision other than the one just registered is deregistered after a successful registration.

### Not supported

- **`entrypoint`** fails the deploy: Batch job definitions have no `entryPoint` field. Put the full
  command in `command`, or bake the `ENTRYPOINT` into the image.
- **`secret_files`** fails the deploy: it needs an init container and a shared volume. Use
  `secrets` / `secrets_envs`.
- Keys with no Batch equivalent — `port`, `additional_ports`, `app_protocol`, `health_check`,
  `sidecars`, `replica_count`, `otel_collector`, `fluent_bit_collector`, `launch_type`,
  `network_mode`, `linux_parameters`, `writable_dirs`, `readonly_root_filesystem`,
  `stop_timeout` — are **ignored with a warning**, so a YAML shared with a service still works.

## Roles

| Precedence | `jobRoleArn` | `executionRoleArn` |
|---|---|---|
| 1 | YAML `task_role_arn` | YAML `execution_role_arn` |
| 2 | YAML `role_arn` | YAML `role_arn` |
| 3 | SSM `/ecs/<cluster>/<task_name>/task-role` | SSM `/ecs/<cluster>/<task_name>/execution-role` |

Step 3 runs only when `ecs_cluster` is set, and then — as for ECS — both slots are mandatory and
an unresolved one fails the deploy. Without `ecs_cluster` there is no SSM fallback: the job role
is optional, and the execution role is required only where Batch needs one (`platform: FARGATE`,
or any secrets).

## Logging

With `ecs_cluster` set, the container logs with `awslogs` to `/ecs/<cluster>/<task_name>`
(stream prefix `batch`) — the same group naming as the ECS types. The log group must exist
(create it in Terraform). Without `ecs_cluster`, no log configuration is set and Batch's default
group `/aws/batch/job` is used.

## IAM permissions

The deploy role (`aws_role`) needs:

| Action | Resource | When |
|---|---|---|
| `batch:RegisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task_name>` (and `…/<task_name>:*`) | always |
| `batch:DescribeJobDefinitions` | `*` (it does not support resource-level permissions) | `deregister_previous: true` |
| `batch:DeregisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task_name>:*` | `deregister_previous: true` |
| `iam:PassRole` | the job role and the execution role | whenever a role is set |
| `ssm:GetParameters` | `arn:aws:ssm:<region>:<account>:parameter/ecs/<cluster>/<task_name>/*` | SSM role fallback |
| `secretsmanager:GetSecretValue` | the named secrets | `secrets_envs` name-only format (key discovery) |

The execution role needs, as for ECS: ECR pull, `logs:CreateLogStream` / `logs:PutLogEvents` on
the log group, and `secretsmanager:GetSecretValue` / `ssm:GetParameters` (plus `kms:Decrypt` for
customer-managed keys) on every secret the job references.

## Example

[examples/batch/batch-ec2.yaml](../examples/batch/batch-ec2.yaml) renders to
[tests/expected_outputs/batch/batch-ec2.json](../tests/expected_outputs/batch/batch-ec2.json);
[examples/batch/batch-fargate.yaml](../examples/batch/batch-fargate.yaml) shows a YAML shared with
a service, with the job's Fargate settings under `services_overrides`.

```yaml
cpu: 2
memory: 4096
command: ["python", "-m", "worker", "--date", "Ref::date"]
envs:
  - LOG_LEVEL: info
secrets:
  - DB_PASSWORD: arn:aws:secretsmanager:us-east-1:123456789012:secret:worker-db
  - API_TOKEN: arn:aws:ssm:us-east-1:123456789012:parameter/worker/api-token
task_role_arn: arn:aws:iam::123456789012:role/worker-job
execution_role_arn: arn:aws:iam::123456789012:role/worker-execution
timeout_seconds: 3600
retry_attempts: 3
parameters:
  date: "today"
```
