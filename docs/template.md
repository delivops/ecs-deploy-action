# Template Mode (`task_definition_source: template`)

In template mode the task definition doesn't come from a YAML file. Terraform manages it, and a deploy only
swaps the image. It works for `deployment_type: service` and, with a template job definition, for
[`deployment_type: batch_job_definition`](#batch-job-definitions).

[`delivops/terraform-aws-ecs-service`](https://github.com/delivops/terraform-aws-ecs-service) (>= 3.2.0) with
`task_definition_template` enabled keeps a fully managed task definition in a separate family,
`<cluster>_<service>-template`. Every task setting lives there: containers, env, secrets, volumes (including
EFS), roles, sidecars, logging. A deploy in template mode:

1. reads the template family name from SSM;
2. describes the family's latest ACTIVE revision;
3. sets `family` to the service's own family, `<cluster>_<service>`;
4. replaces the image of one container (`container_name`, default `app`);
5. removes the read-only fields (`taskDefinitionArn`, `revision`, `status`, `requiresAttributes`,
   `compatibilities`, `registeredAt`, `registeredBy`, `deregisteredAt`, `deleteRequestedAt`);
6. registers the result and rolls the service out through the usual deploy, stability wait and deployment
   check.

Nothing else changes: no merging, no defaults, no tags. A config change (a new env var, a volume) is a Terraform
apply to the template, and it reaches the service on the next deploy.

## SSM contract

| Parameter | Written by the module | Read by the action |
|---|---|---|
| `/ecs/<cluster>/<service>/task-definition-template` | always, when `task_definition_template` is enabled: the template family name | required; missing means the template isn't enabled, and the deploy fails with a message saying so |
| `/ecs/<cluster>/<service>/replica-count` | only when `replica_count` is set | when it exists, its value is the service's desired count; when it doesn't, the count is left alone (autoscaled services) |

Any error reading `replica-count` other than "not found" fails the deploy, so a missing IAM grant can't
silently skip a count.

## Usage

```yaml
- name: Deploy
  uses: delivops/ecs-deploy-action@<tag>
  with:
    environment: production
    deployment_type: service
    ecs_service: my-service
    ecs_cluster: my-cluster
    image_name: my-app
    tag: ${{ github.sha }}
    task_definition_source: template
    container_name: app          # default
    aws_account_id: ${{ secrets.AWS_ACCOUNT_ID }}
    aws_region: us-east-1
```

- `task_config_yaml` must be left out. Setting it with `template` fails the deploy before any AWS call.
- `deployment_type` must be `service` here, or `batch_job_definition` ([below](#batch-job-definitions)); the
  other types fail before any AWS call.
- `dry_run: 'true'` writes and prints `task-definition.json` without registering or deploying, as in YAML mode.
- `ecr_registry` and `image_name` build the image reference exactly as in YAML mode (the same code), so a public
  image or a registry/tag embedded in `image_name` works the same way.

## IAM

The deploy role needs:

| Action | Resource |
|---|---|
| `ssm:GetParameter` | `arn:aws:ssm:<region>:<account>:parameter/ecs/*/*/task-definition-template` and `.../ecs/*/*/replica-count` |
| `ecs:DescribeTaskDefinition`, `ecs:RegisterTaskDefinition` | `*` |
| `ecs:UpdateService`, `ecs:DescribeServices` | the service |
| `iam:PassRole` | the template's task and execution roles |

Not needed in template mode: `ecs:TagResource`, and the `task-role` / `execution-role` SSM parameters (the
roles come with the template).

## Tags

Tags aren't copied from the template. Terraform tags the service and sets `propagate_tags = "SERVICE"`, and the
deploy step keeps that setting (see [Task Tagging](../README.md#task-tagging)).

## Batch job definitions

With `deployment_type: batch_job_definition`, the same idea applies to an AWS Batch job definition. Terraform
manages a complete *template* job definition (type `container`, EC2 or Fargate): command, env, secrets, roles,
resources, timeout, retry strategy, parameters, logging. A deploy:

1. reads the template's name from SSM `/batch/<task_name>/job-definition-template`;
2. describes the template's ACTIVE revisions and takes the highest;
3. sets `jobDefinitionName` to `task_name`;
4. replaces `containerProperties.image`;
5. removes the read-only fields (`jobDefinitionArn`, `revision`, `status`, `containerOrchestrationType`) and
   `tags`;
6. registers the result as a new revision of `task_name`, and deregisters older ones when `deregister_previous`
   is `true`.

Whatever submits the jobs references `task_name`, so it always gets the latest revision.

**The template must be a separate job definition from `task_name`.** If Terraform managed `task_name` itself,
its next apply would register a revision with the stale image it last saw. The deploy fails if the SSM parameter
names `task_name`.

Only single-container job definitions are supported: a template with `ecsProperties`, `eksProperties` or
`nodeProperties` (multi-node) fails the deploy with a message saying so.

### SSM contract

| Parameter | Read by the action |
|---|---|
| `/batch/<task_name>/job-definition-template` | required: the template job definition's **name** (not an ARN, not `name:revision`); missing fails the deploy |

There is no Batch equivalent of the ECS module that publishes it, so the Terraform that creates the template
writes it:

```hcl
resource "aws_batch_job_definition" "template" {
  name                  = "nightly-report-template"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  propagate_tags        = true
  parameters            = { date = "today" }
  retry_strategy { attempts = 3 }
  timeout { attempt_duration_seconds = 3600 }

  container_properties = jsonencode({
    image            = "${aws_ecr_repository.app.repository_url}:template" # replaced on every deploy
    command          = ["python", "-m", "worker", "--date", "Ref::date"]
    jobRoleArn       = aws_iam_role.job.arn
    executionRoleArn = aws_iam_role.execution.arn
    resourceRequirements = [
      { type = "VCPU", value = "1" },
      { type = "MEMORY", value = "2048" },
    ]
    environment = [{ name = "LOG_LEVEL", value = "info" }]
    secrets     = [{ name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db.arn}:DB_PASSWORD::" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.job.name
        awslogs-region        = "us-east-1"
        awslogs-stream-prefix = "batch"
      }
    }
    networkConfiguration = { assignPublicIp = "DISABLED" }
  })
}

resource "aws_ssm_parameter" "job_definition_template" {
  name  = "/batch/nightly-report/job-definition-template"
  type  = "String"
  value = aws_batch_job_definition.template.name
}
```

The values in the template are Batch's own (`resourceRequirements` `VCPU` in vCPUs, not ECS CPU units): the
action copies them as they are.

### Usage

```yaml
- name: Register Batch job definition
  id: batch
  uses: delivops/ecs-deploy-action@<tag>
  with:
    environment: production
    deployment_type: batch_job_definition
    task_definition_source: template
    task_name: nightly-report      # the job definition jobs are submitted to
    image_name: nightly-report
    tag: ${{ github.sha }}
    deregister_previous: 'false'   # default
    aws_account_id: ${{ secrets.AWS_ACCOUNT_ID }}
    aws_region: us-east-1

- run: echo "Registered ${{ steps.batch.outputs.job_definition_arn }}"
```

- `task_config_yaml` must be left out, as for `service`; setting it fails the deploy before any AWS call.
- `deregister_previous` is an action input here, because there is no YAML. In YAML mode it stays a YAML key, and
  setting the input there fails the deploy rather than being silently ignored.
- `container_name` and `ecs_cluster` are not used.
- `dry_run: 'true'` reads the template (so it needs the read permissions below), writes and prints
  `job-definition.json`, and registers nothing.
- `job_definition_arn` is the new revision's ARN, as in YAML mode.
- `ecr_registry`, `image_name` and `tag` build the image reference with the same code as every other mode.

### IAM

The deploy role needs:

| Action | Resource | When |
|---|---|---|
| `ssm:GetParameter` | `arn:aws:ssm:<region>:<account>:parameter/batch/*/job-definition-template` | always |
| `batch:DescribeJobDefinitions` | `*` (it does not support resource-level permissions) | always |
| `batch:RegisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task_name>` (and `…/<task_name>:*`) | always |
| `batch:DeregisterJobDefinition` | `arn:aws:batch:<region>:<account>:job-definition/<task_name>:*` | `deregister_previous: 'true'` |
| `iam:PassRole` | the template's job role and execution role | whenever the template sets them |

Not needed: the `/batch/<task_name>/job-role` and `/execution-role` SSM parameters (the roles come with the
template), and `secretsmanager:*` (secrets are copied as they are, never looked up).

### Tags

Tags aren't copied from the template, as for `service`, so the registered revision is untagged. The
template's `propagateTags` is copied; with `true`, Batch copies the tags of the job and of the job definition
to the ECS task, which here means the tags set when the job is submitted.
