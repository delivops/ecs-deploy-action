# Waiting for a service deployment (`wait_for`, `deployment_type: wait`)

By default a `service` deploy waits for ECS's **steady state**: the new tasks are running, the old tasks have
stopped (including their `stopTimeout` and the load balancer's deregistration delay), and ECS has marked the
deployment complete. That is often several minutes after the new revision started serving. If a workflow deploys
services one after another, those minutes add up on the critical path.

`wait_for` controls what a `service` deploy waits for. `deployment_type: wait` waits for several deployments at
once, so a workflow can roll every service, move on as soon as each one serves, and confirm they all completed in
one step at the end.

## `wait_for`

| `wait_for` | Deploy step | Then |
|---|---|---|
| `stable` (default) | `wait-for-service-stability: true` | the deployment check, exactly as before these inputs existed |
| `healthy` | `wait-for-service-stability: false` | polls every 5 s until the new revision **serves** (below), then returns |
| `none` | `wait-for-service-stability: false` | checks that the new revision's deployment is PRIMARY, then returns |

`wait_timeout_minutes` (default `30`, 1–360) applies to `stable` (it is the upstream `wait-for-minutes`) and to
`healthy`. When the time runs out, the step fails, but the ECS deployment carries on.

### What "serving" means (`healthy`)

The deployment of the revision this run registered (found by its task definition ARN, not by its position in the
list) must meet all of the following:

- it is **PRIMARY**, and its `rolloutState` is not `FAILED`;
- its `runningCount` reaches the service's `desiredCount` (a service with a desired count of 0 is done at once);
- if an essential container in the task definition has a `healthCheck`: every task of the deployment reports
  `healthStatus: HEALTHY`;
- if the service has load balancers: every task of the deployment is `healthy` in each target group (matched by
  the task's ENI private IP and the container port).

The step does **not** wait for the old tasks to drain or stop, or for ECS to report the deployment `COMPLETED`.

Failures:

| What happens | Result |
|---|---|
| `rolloutState` becomes `FAILED`, or another deployment becomes PRIMARY (a circuit-breaker rollback or a newer deploy) | fails at once, with `rolloutStateReason`, the stopped tasks' `stoppedReason` and the last service events |
| `failedTasks` > 0 and the service has a **circuit breaker** | a warning with the `stoppedReason`. The step keeps waiting, because the breaker allows a few failed tasks and decides whether to roll back |
| `failedTasks` > 0 and **no circuit breaker** | fails at once, because nothing else would stop the rollout |
| the service uses the `CODE_DEPLOY` or `EXTERNAL` deployment controller | fails: use `stable` |

Fallbacks, each with a warning instead of a failure:

- `ecs:ListTasks` or `ecs:DescribeTasks` is denied: only `runningCount` is checked (no container or target health).
- `elasticloadbalancing:DescribeTargetHealth` is denied: target health is not checked.
- The tasks have no ENI (`bridge` or `host` network mode, instance targets), or the service uses a classic load
  balancer: that load balancer is not checked.

### The trade-off

`healthy` and `none` return **before ECS has finished the deployment**. The circuit breaker (if enabled) keeps
protecting the rollout, but a rollback that happens after the step has returned shows up only in ECS, not in the
workflow. A workflow that uses `healthy` or `none` should therefore end with a `deployment_type: wait` step on the
deployments it started. If it doesn't, you accept that a late rollback is visible only in ECS.

`wait_for` does not change the service's `deploymentConfiguration` (circuit breaker, minimum/maximum percent).
Those stay with whatever manages the service, such as Terraform.

## `deployment_type: wait`

Deploys nothing. It waits until every listed deployment completes.

| Input | |
|---|---|
| `ecs_cluster` | the cluster of all the services |
| `services` | newline- or comma-separated: `name=<deployment_id>` (the `deployment_id` output of the deploy) or just `name` (its PRIMARY deployment at the first poll) |
| `wait_timeout_minutes` | default `30` |
| `aws_account_id`, `aws_region`, `aws_role` | as for the other types |

`environment`, `image_name` and `tag` are not used. Every 10 s it calls `DescribeServices` once per 10 services
(the API's maximum per call), and:

- **succeeds** when every deployment's `rolloutState` is `COMPLETED`;
- **fails at once** when any deployment is `FAILED`, when it is no longer the service's PRIMARY (it was rolled back
  or replaced), or when the service is missing. It also fails when the timeout passes. The error names each
  service, the reason (`rolloutStateReason`) and its last 5 service events.

It writes a table to the job summary:

| service | deployment | state | time to complete |
|---|---|---|---|
| api | `ecs-svc/1234567890123456789` | ✅ COMPLETED | 2m35s |
| worker | `ecs-svc/2345678901234567890` | ✅ COMPLETED | 3m10s |

"Time to complete" runs from the deployment's creation to ECS marking it complete.

## Example: deploy in parallel, wait once at the end

```yaml
jobs:
  api:
    runs-on: ubuntu-latest
    permissions: { id-token: write, contents: read }
    outputs:
      deployment_id: ${{ steps.deploy.outputs.deployment_id }}
    steps:
      - uses: actions/checkout@v4
      - id: deploy
        uses: delivops/ecs-deploy-action@<sha>   # v0.7.0 or later
        with:
          environment: production
          ecs_cluster: my-cluster
          ecs_service: api
          image_name: my-app
          tag: ${{ github.sha }}
          task_config_yaml: config/api.yaml
          aws_account_id: ${{ vars.AWS_ACCOUNT_ID }}
          aws_region: us-east-1
          wait_for: healthy

  worker:
    # ... the same with ecs_service: worker

  deployed:
    needs: [api, worker]
    runs-on: ubuntu-latest
    permissions: { id-token: write, contents: read }
    steps:
      - uses: delivops/ecs-deploy-action@<sha>
        with:
          deployment_type: wait
          ecs_cluster: my-cluster
          aws_account_id: ${{ vars.AWS_ACCOUNT_ID }}
          aws_region: us-east-1
          services: |
            api=${{ needs.api.outputs.deployment_id }}
            worker=${{ needs.worker.outputs.deployment_id }}
```

## Outputs

| Output | |
|---|---|
| `task_definition_arn` | the revision this run registered (`service`, `scheduled_task`, `triggerable_task`) |
| `deployment_id` | `service`: the ECS deployment id (`ecs-svc/…`) of that revision, in every `wait_for` mode |

Both outputs are empty on `dry_run`.

## IAM

On top of the [`service` permissions](permissions.md#per-deployment-type):

| Action | Resource | When |
|---|---|---|
| `ecs:DescribeServices` | the service(s) | every `wait_for` mode (already needed by `service`) and `deployment_type: wait` |
| `ecs:ListTasks` | `*`, with condition `"ArnEquals": {"ecs:cluster": "arn:aws:ecs:<region>:<account>:cluster/<cluster>"}` | `healthy`, for container or target health, and to show stopped tasks' reasons |
| `ecs:DescribeTasks` | `arn:aws:ecs:<region>:<account>:task/<cluster>/*` | the same |
| `elasticloadbalancing:DescribeTargetHealth` | `*` (no resource-level permissions) | `healthy`, load-balanced services |

`deployment_type: wait` needs only `ecs:DescribeServices` (no `ecr:GetAuthorizationToken`: it skips the ECR login).
