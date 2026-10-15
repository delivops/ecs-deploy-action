# AWS Batch Job Definitions

`deployment_type: batch_job_definition` registers a new revision of an AWS Batch job definition
from the same `task_config_yaml` format the ECS deployment types use. The image is built from
`image_name` + `tag` exactly as for ECS, and `task_name` is the job definition name.

It is opt-in: nothing changes for `service`, `scheduled_task`, `triggerable_task` or template mode.

The action only registers the job definition. The compute environment, job queue, log group and
IAM roles stay in Terraform, and whatever submits the jobs (a schedule, Step Functions, another
workflow) references the job definition by name, which always resolves to its latest ACTIVE
revision.

A job definition name is unique per AWS account and region — Batch has no cluster/service pair
like ECS — so `task_name` is the job's whole identity: roles fall back to SSM
`/batch/<task_name>/…` and logs go to `/batch/<task_name>`. Nothing about a Batch job uses
`ecs_cluster` or the `/ecs/…` conventions. Two environments sharing one account need different
`task_name`s (e.g. `prod-report`, `staging-report`), exactly as Batch itself requires.

## Usage

```yaml
- name: Register Batch job definition
  id: batch
  uses: delivops/ecs-deploy-action@v1
  with:
    environment: production
    deployment_type: batch_job_definition
    task_name: nightly-report          # the job definition name
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
| `ecs_cluster` | Not used. |
| `dry_run` | `true` writes and prints `job-definition.json` and registers nothing. |

Output: `job_definition_arn` — the ARN of the new revision (empty on a dry run).

## YAML mapping

Existing keys are reused where they mean the same thing:

| YAML key | Job definition | Notes |
|---|---|---|
| `cpu` | `resourceRequirements` `VCPU` | **ECS CPU units, as for every type** (1024 = 1 vCPU), converted to vCPUs. Default `1024`. |
| `memory` | `resourceRequirements` `MEMORY` | MiB. Default `2048`. On Fargate it must be a valid combination with `cpu` (same table as ECS Fargate). |
| `command` | `containerProperties.command` | Omitted when empty, so the image's `CMD` applies. |
| `envs`, `envs_from_files` | `containerProperties.environment` | Same rules as ECS. |
| `secrets`, `secrets_envs` (both formats) | `containerProperties.secrets` | All three formats, including SSM parameter ARNs — see [secrets.md](secrets.md). |
| `task_role_arn` / `execution_role_arn` / `role_arn` | `jobRoleArn` / `executionRoleArn` | YAML first, then SSM `/batch/<task_name>/…` — see [Roles](#roles). |
| `cpu_arch` | `runtimePlatform.cpuArchitecture` | Fargate only (`X86_64` default, or `ARM64`). On EC2 the compute environment decides. |
| `ephemeral_storage` | `ephemeralStorage.sizeInGiB` | Fargate only, 21–200. |
| `services_overrides.<task_name>` | — | Applied with `task_name` as the key, exactly like a service name. |

Batch-only keys:

| YAML key | Job definition | Default |
|---|---|---|
| `platform` | `platformCapabilities` | `EC2` (or `FARGATE`) |
| `log_group` | `logConfiguration` `awslogs-group` | `/batch/<task_name>` |
| `timeout_seconds` | `timeout.attemptDurationSeconds` | none; at least 60 |
| `retry_attempts` | `retryStrategy.attempts` | `1`; 1–10 |
| `parameters` | `parameters` (defaults for `Ref::<name>`) | none |
| `propagate_tags` | `propagateTags` | `true` |
| `deregister_previous` | deregisters older ACTIVE revisions after registering | `false` |

### `cpu` is in CPU units

`cpu` means the same thing for every deployment type: ECS CPU units, 1024 = 1 vCPU. The action
divides by 1024 for Batch, so a YAML shared with a service needs no translation:

| `cpu` | Batch vCPUs | Platform |
|---|---|---|
| `256` / `512` | 0.25 / 0.5 | Fargate only |
| `1024`, `2048`, `4096`, … | 1, 2, 4, … | EC2 and Fargate |

AWS Batch on EC2 takes whole vCPUs, so there `cpu` must be a multiple of 1024 (the error names
the nearest valid values). On Fargate, `cpu` and `memory` must be one of the Fargate
combinations — the same table as ECS Fargate, plus 32768 (32 vCPUs). A size for the job only
goes under its `services_overrides` entry:

```yaml
cpu: 512          # the service, and the job unless overridden: 0.5 vCPU
memory: 1024
services_overrides:
  nightly-report: # the job (task_name)
    platform: FARGATE
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

The job takes the same two IAM roles as an ECS task — both trust `ecs-tasks.amazonaws.com`,
because Batch runs the container on ECS underneath. Each slot is resolved on its own, first match
wins:

| Precedence | `jobRoleArn` | `executionRoleArn` |
|---|---|---|
| 1 | YAML `task_role_arn` | YAML `execution_role_arn` |
| 2 | YAML `role_arn` | YAML `role_arn` |
| 3 | SSM `/batch/<task_name>/job-role` | SSM `/batch/<task_name>/execution-role` |

The SSM lookup is one batched `GetParameters` call, skipped when the YAML sets both slots. Publish
the parameters from the Terraform that creates the roles, for example:

```hcl
resource "aws_ssm_parameter" "job_role" {
  name  = "/batch/nightly-report/job-role"
  type  = "String"
  value = aws_iam_role.job.arn
}
```

A slot that is still empty is not automatically an error: the job role is optional (a job that
calls no AWS APIs needs none), and the execution role is required only where Batch needs one —
`platform: FARGATE`, or any secrets. Then the deploy fails, naming both the YAML key and the SSM
parameter it tried. The ECS path `/ecs/<cluster>/<name>/…` is never read for a Batch job.

## Logging

Every job definition logs with `awslogs` to its own group: `/batch/<task_name>` by default, or the
YAML `log_group` (which can differ per job under `services_overrides`). The stream prefix is
`batch`. The group must exist — create it in Terraform with a retention period; the action never
creates it, so it can't leave a group that keeps logs forever.

## IAM permissions

The deploy role (`aws_role`) needs:

| Action | Resource | When |
|---|---|---|
| `batch:RegisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task_name>` (and `…/<task_name>:*`) | always |
| `batch:DescribeJobDefinitions` | `*` (it does not support resource-level permissions) | `deregister_previous: true` |
| `batch:DeregisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task_name>:*` | `deregister_previous: true` |
| `iam:PassRole` | the job role and the execution role | whenever a role is set |
| `ssm:GetParameters` | `arn:aws:ssm:<region>:<account>:parameter/batch/<task_name>/*` | whenever the YAML doesn't set both roles |
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
cpu: 2048          # 2 vCPUs
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
