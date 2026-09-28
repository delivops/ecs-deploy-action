# Template Mode (`task_definition_source: template`)

In template mode the task definition doesn't come from a YAML file. Terraform manages it, and a deploy only
swaps the image.

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
- Only `deployment_type: service` is supported.
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
