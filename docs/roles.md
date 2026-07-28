# Task and Execution Roles

An ECS task definition carries two IAM roles:

| Role | Used by | Needed for |
|---|---|---|
| `taskRoleArn` | your application code, inside the container | the AWS APIs your app calls (S3, SQS, DynamoDB…) |
| `executionRoleArn` | the ECS agent, before your container starts | pulling the image from ECR, writing `awslogs`, reading secrets |

They are different identities with different jobs, and both are required. The same ARN may be used
for both, but neither slot may be left empty.

## Why this changed

Up to `delivops/terraform-aws-ecs-service` v2.x, both slots were derived from a single shared
`role` variable, so a pipeline could read one ARN and use it twice without anyone noticing.

**v3.0.0 replaced that with two symmetric objects, `task_role` and `execution_role`**, each with
its own `create` / `arn` / `inline_policy` / `attach_policies`. With `execution_role.create = true`
the module provisions a *second* role named `<cluster>_<service>_execution`. Reusing one ARN for
both slots is now wrong, so this action resolves each slot independently.

## Resolution order

Each slot is resolved on its own, first match wins:

| Precedence | `taskRoleArn` | `executionRoleArn` |
|---|---|---|
| 1 | YAML `task_role_arn` | YAML `execution_role_arn` |
| 2 | YAML `role_arn` | YAML `role_arn` |
| 3 | SSM `/ecs/<cluster>/<service>/task-role` | SSM `/ecs/<cluster>/<service>/execution-role` |

`<cluster>` is the `ecs_cluster` action input; `<service>` is the `ecs_service` input (or
`task_name` for task deployments). These are the same identifiers the action already uses for the
task definition family `<cluster>_<service>` and the log group `/ecs/<cluster>/<service>`.

The SSM lookup is a single batched `GetParameters` call, and it is skipped entirely when both
slots are already resolved from YAML — a fully YAML-configured deploy makes no AWS calls to
resolve roles.

## The three configurations

### 1. Discovered from SSM (recommended for module-managed services)

```yaml
cpu: 256
memory: 512
port: 8080
```

No role configuration at all. The module publishes both parameters; the action reads them. Roles
rotate in Terraform without touching this file.

### 2. One shared role

```yaml
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
```

Both slots get the same ARN. This is the pre-v3 behavior and remains fully supported.

### 3. Separate roles

```yaml
task_role_arn: arn:aws:iam::123456789012:role/my-cluster_my-service
execution_role_arn: arn:aws:iam::123456789012:role/my-cluster_my-service_execution
```

Neither key falls back to the other. Mixing is fine — `role_arn` covers whatever a per-slot key
does not:

```yaml
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
execution_role_arn: arn:aws:iam::123456789012:role/my-cluster_my-service_execution
```

Here the task role comes from `role_arn` and the execution role is overridden.

## Clearing a value in `services_overrides`

Setting a role key to YAML `null` **removes** it, so resolution falls through to the next
precedence level rather than leaving the slot empty. This only does something when the base
config sets the key in the first place:

```yaml
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
task_role_arn: arn:aws:iam::123456789012:role/my-cluster_shared-task

services_overrides:
  api-service:
    task_role_arn: arn:aws:iam::123456789012:role/my-cluster_api   # replace the base task role
  worker-service:
    task_role_arn: null                                            # drop the base task role,
                                                                   # fall back to role_arn
```

Resolves to:

| Service | `taskRoleArn` | `executionRoleArn` |
|---|---|---|
| `api-service` | `my-cluster_api` | `ecsTaskExecutionRole` |
| `worker-service` | `ecsTaskExecutionRole` | `ecsTaskExecutionRole` |
| any other service | `my-cluster_shared-task` | `ecsTaskExecutionRole` |

Note that `task_role_arn` narrows only the task slot — the execution slot keeps coming from
`role_arn`.

Watch out for `task_role_arn: no`, which YAML 1.1 parses as the boolean `false`. The action
rejects it with an explanatory error rather than letting it fail somewhere downstream.

## IAM prerequisite

For the SSM fallback, the deploy role (the `aws_role` action input, default `github_services`)
needs:

```json
{
  "Effect": "Allow",
  "Action": "ssm:GetParameters",
  "Resource": [
    "arn:aws:ssm:<region>:<account-id>:parameter/ecs/*/*/task-role",
    "arn:aws:ssm:<region>:<account-id>:parameter/ecs/*/*/execution-role"
  ]
}
```

`ssm:GetParameters` is **plural** — the action makes one batched call, so granting only the
singular `ssm:GetParameter` will not work.

This permission is only required for the SSM fallback. If it cannot be granted, keep specifying
`role_arn` (or the per-slot keys) in YAML and nothing changes.

## Scheduled and triggerable tasks

`deployment_type: scheduled_task` and `triggerable_task` are **not** managed by the ECS service
module, so no `/ecs/<cluster>/<task_name>/…` parameters exist for them. The SSM fallback can never
succeed, and the deploy will fail with a message saying so.

**Set the role ARNs in YAML for these deployments:**

```yaml
role_arn: arn:aws:iam::123456789012:role/my-cluster_my-task
```

## Failure modes

Roles are never guessed. If a slot cannot be resolved the deploy fails before registering
anything, rather than producing a task definition that would fail at launch or run with the wrong
permissions.

| Situation | Behavior |
|---|---|
| Slot unresolved from all three sources | Fail, listing each source tried and how to fix it |
| Both slots unresolved | Fail, reporting both at once |
| No AWS credentials | Fail naming the missing parameters |
| Credentials expired or invalid | Fail naming the error code |
| No AWS region configured | Fail naming the region that was used |
| SSM endpoint unreachable | Fail naming the region and the underlying network error |
| `AccessDenied` | Fail naming `ssm:GetParameters` and the required resource ARN |
| Throttled | Fail suggesting a retry |
| Value is a role *name*, not an ARN | Fail — the module validates this on its side too |
| Parameter exists but is empty | Treated as missing |
| Value parsed as a YAML boolean (`no`, `off`) | Fail explaining the YAML 1.1 quirk |

A note on why this is stricter than elsewhere in the action: `SecretManager` falls back to mock
values when AWS is unreachable, which is tolerable for discovering secret *keys* during local
runs. Roles are different — silently deploying with the wrong or no role is worse than a failed
deploy, so `RoleResolver` never substitutes a fallback value.

## Examples

| File | Shows |
|---|---|
| [`examples/separate-roles.yaml`](../examples/separate-roles.yaml) | distinct task and execution roles |
| [`examples/role-arn-with-override.yaml`](../examples/role-arn-with-override.yaml) | shared `role_arn` with one slot overridden |
| [`examples/multi-service-roles.yaml`](../examples/multi-service-roles.yaml) | per-service role overrides |
