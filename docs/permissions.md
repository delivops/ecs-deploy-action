# IAM Permissions

Three IAM identities are involved in a deployment. Each needs only what is listed here.

| Identity | Who uses it | When |
|---|---|---|
| **Deploy role** (`aws_role` input) | this action, in the GitHub workflow | while deploying |
| **Execution role** (`execution_role_arn`) | ECS / Batch, before your container starts | pulling the image, writing logs, injecting secrets |
| **Task role** / **job role** (`task_role_arn`) | your code, inside the container | the AWS APIs your application calls |

Placeholders below: `<region>`, `<account>`, `<cluster>` (`ecs_cluster`), `<service>` (`ecs_service`),
`<task>` (`task_name`), and `<exec-role>` / `<task-role>` (the role names).

## Deploy role

### Every deployment type

| Action | Resource | Why |
|---|---|---|
| `ecr:GetAuthorizationToken` | `*` | ECR login, which runs on every deploy |

The role's **trust policy** must allow `sts:AssumeRoleWithWebIdentity` from GitHub's OIDC provider
(`token.actions.githubusercontent.com`) for your repository — that is a trust relationship, not a
permission.

### Per deployment type

| Action | Resource | `service` | `scheduled_task` | `triggerable_task` | `batch_job_definition` |
|---|---|:-:|:-:|:-:|:-:|
| `ecs:RegisterTaskDefinition` | `*` | ✅ | ✅ | ✅ | |
| `ecs:UpdateService`, `ecs:DescribeServices` | `arn:aws:ecs:<region>:<account>:service/<cluster>/<service>` | ✅ | | | |
| `events:ListTargetsByRule`, `events:PutTargets` | `arn:aws:events:<region>:<account>:rule/<cluster>-<task>` | | ✅ | | |
| `iam:PassRole` on the execution and task roles | `arn:aws:iam::<account>:role/<exec-role>`, `…/<task-role>` | ✅ | ✅ | ✅ | ✅ |
| `iam:PassRole` on the EventBridge target's role | the rule target's `RoleArn` | | ✅ | | |
| `batch:RegisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task>`, `…/<task>:*` | | | | ✅ |
| `batch:DescribeJobDefinitions` | `*` (no resource-level permissions) | | | | `deregister_previous: true` only |
| `batch:DeregisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task>:*` | | | | `deregister_previous: true` only |

- `scheduled_task` rewrites the rule's existing target (only its task definition changes), and
  `PutTargets` re-sends the target's `RoleArn` — that is why the deploy role must be allowed to
  pass the EventBridge role as well.
- For the ECS types, `iam:PassRole` can be narrowed with the condition
  `"iam:PassedToService": "ecs-tasks.amazonaws.com"` (and `events.amazonaws.com` for the
  EventBridge role).
- `dry_run: true` skips every registration and update, so it needs only `ecr:GetAuthorizationToken`
  and the YAML-dependent reads below.
- [Template mode](template.md#iam) (`task_definition_source: template`) has its own list, for
  [`service`](template.md#iam) and for [`batch_job_definition`](template.md#iam-1).

### Depending on the YAML

| Action | Resource | When |
|---|---|---|
| `ssm:GetParameters` | `arn:aws:ssm:<region>:<account>:parameter/ecs/<cluster>/<service-or-task>/*` | ECS types, when the YAML doesn't set both roles ([roles.md](roles.md)) |
| `ssm:GetParameters` | `arn:aws:ssm:<region>:<account>:parameter/batch/<task>/*` | `batch_job_definition`, when the YAML doesn't set both roles |
| `secretsmanager:DescribeSecret` | each secret named by a `secrets_envs` entry with `name` + `env` | metadata only — the value is never read |
| `secretsmanager:GetSecretValue` | each secret named by a name-only `secrets_envs` entry | key discovery reads the secret to list its JSON keys |
| `kms:Decrypt` | the secret's customer-managed KMS key | name-only key discovery on a secret encrypted with your own key |

Nothing else in the YAML needs a deploy-role permission: `secrets`, `secrets_envs` with `id`, and
SSM parameter ARNs are written into the definition without being looked up.

### Example: `service`, roles and secrets set in YAML

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Effect": "Allow", "Action": "ecs:RegisterTaskDefinition", "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": ["ecs:UpdateService", "ecs:DescribeServices"],
      "Resource": "arn:aws:ecs:us-east-1:123456789012:service/my-cluster/my-service"
    },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::123456789012:role/my-service-task",
        "arn:aws:iam::123456789012:role/my-service-execution"
      ],
      "Condition": { "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" } }
    }
  ]
}
```

### Example: `batch_job_definition`, roles from SSM, `deregister_previous: true`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameters",
      "Resource": "arn:aws:ssm:us-east-1:123456789012:parameter/batch/nightly-report/*"
    },
    {
      "Effect": "Allow",
      "Action": ["batch:RegisterJobDefinition", "batch:DeregisterJobDefinition"],
      "Resource": [
        "arn:aws:batch:us-east-1:123456789012:job-definition/nightly-report",
        "arn:aws:batch:us-east-1:123456789012:job-definition/nightly-report:*"
      ]
    },
    { "Effect": "Allow", "Action": "batch:DescribeJobDefinitions", "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::123456789012:role/nightly-report-job",
        "arn:aws:iam::123456789012:role/nightly-report-execution"
      ]
    }
  ]
}
```

## Execution role

Used by ECS (or by Batch, which runs the container on ECS) before your code starts. It trusts
`ecs-tasks.amazonaws.com`. The same list applies to every deployment type.

| Action | Resource | When |
|---|---|---|
| `ecr:GetAuthorizationToken` | `*` | image in ECR |
| `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchCheckLayerAvailability` | the ECR repositories of the app, sidecar, Fluent Bit and OTEL images | image in ECR |
| `logs:CreateLogStream`, `logs:PutLogEvents` | the log group, e.g. `arn:aws:logs:<region>:<account>:log-group:/ecs/<cluster>/<service>:*` (Batch: `/batch/<task>`, or `log_group`) | always — every container logs with `awslogs` |
| `secretsmanager:GetSecretValue` | every Secrets Manager secret referenced by `secrets` or `secrets_envs` (any form), for the app and every sidecar | any secret |
| `ssm:GetParameters` | every SSM parameter referenced by `secrets`, and the OTEL config parameter (`otel_collector.ssm_name`) when the default OTEL image is used | any SSM parameter |
| `kms:Decrypt` | the customer-managed KMS key of those secrets / SecureString parameters | only with your own KMS key |

The AWS managed policy `AmazonECSTaskExecutionRolePolicy` covers the ECR and logs rows (on `*`);
add the secrets rows yourself. Secrets Manager adds a 6-character suffix to every secret ARN, so
match it with `-??????`:
`arn:aws:secretsmanager:<region>:<account>:secret:my-secret-??????`.

On Batch, the execution role is required for Fargate jobs and for any job with secrets; an EC2 job
without one writes its logs with the container instance role instead.

## Task role / job role

Whatever your application calls (S3, SQS, DynamoDB…), plus what this action adds inside the task:

| Action | Resource | When |
|---|---|---|
| `secretsmanager:GetSecretValue` (+ `kms:Decrypt` for your own key) | each secret in `secret_files` | `secret_files` — the init container downloads them with the **task** role, not the execution role (ECS only) |
| the permissions of your Fluent Bit outputs | e.g. `logs:*` / `firehose:PutRecordBatch` on the destination | `fluent_bit_collector`, depending on its configuration |
| the permissions of your OTEL exporters | e.g. `cloudwatch:PutMetricData`, `xray:PutTraceSegments` | `otel_collector`, depending on its configuration |

The task role is optional for a Batch job that calls no AWS APIs.
