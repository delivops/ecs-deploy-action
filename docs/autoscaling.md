# Autoscaling Configuration

The ECS Deploy Action supports automatic publishing of autoscaling configurations to DynamoDB. This feature enables declarative autoscaling definitions that live alongside your service deployment configuration.

## Overview

When you include an `autoscaling_configs` block in your task configuration YAML, the action will:

1. **Validate** the configuration against a JSON schema
2. **Publish** it to DynamoDB table `${ecs_cluster}_ecs_autoscaling_config` atomically
3. **Continue** the deployment even if publishing fails (non-blocking)

If the `autoscaling_configs` block is absent, the action does nothing (opt-out by omission).

## Quick Start

Add an `autoscaling_configs` block to your existing deployment YAML (e.g., `.aws/production.yaml`):

```yaml
# ... existing ECS task config ...

autoscaling_configs:
  provider:
    type: sqs
    sqs:
      queue_url: https://sqs.us-east-1.amazonaws.com/123456789012/my-queue
  
  min_tasks: 2
  max_tasks: 50
  target_max_message_age_seconds: 120
  scale_out_cooldown_seconds: 90
  scale_in_cooldown_seconds: 600
```

No workflow changes required! The action automatically detects and publishes the configuration.

## Provider Types

The `provider.type` field determines which autoscaling strategy to use:

### 1. SQS-based (`sqs`)

Scale based on SQS queue metrics (message age, visible messages).

```yaml
autoscaling_configs:
  provider:
    type: sqs
    sqs:
      queue_url: https://sqs.us-east-1.amazonaws.com/123456789012/production-queue
  
  min_tasks: 2
  max_tasks: 200
  target_max_message_age_seconds: 120
  scale_out_cooldown_seconds: 90
  scale_in_cooldown_seconds: 600
  max_scale_out_percent: 25
  max_scale_in_tasks: 1
  scale_in_guard:
    mode: empty_queue
```

**Required fields when using SQS:**
- `sqs.queue_url` - Full SQS queue URL

### 2. Time-based (`time`)

Scale based on time schedules (business hours, weekends, etc.).

```yaml
autoscaling_configs:
  provider:
    type: time
    time:
      timezone: America/New_York
      mode: floor  # floor | ceiling | override | scale_in_only
      rules:
        - days: [mon, tue, wed, thu, fri]
          start: "09:00"
          end: "18:00"
          min_desired: 10
        - days: [sat, sun]
          min_desired: 1
  
  min_tasks: 1
  max_tasks: 50
```

**Time modes:**
- `floor` - Set minimum capacity (default behavior still applies above this)
- `ceiling` - Set maximum capacity
- `override` - Directly set desired capacity (requires `desired` field in rules)
- `scale_in_only` - Only allow scaling in during time windows

**Time format:** `HH:MM` (24-hour format, e.g., `"09:00"`, `"17:30"`)

**Days:** `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`

### 3. Combined SQS + Time (`sqs+time`)

Best of both worlds: reactive SQS scaling with scheduled capacity adjustments.

```yaml
autoscaling_configs:
  provider:
    type: sqs+time
    sqs:
      queue_url: https://sqs.us-east-1.amazonaws.com/123456789012/my-queue
    time:
      timezone: America/New_York
      mode: floor
      rules:
        - days: [mon, tue, wed, thu, fri]
          start: "09:00"
          end: "18:00"
          min_desired: 4
        - days: [sat, sun]
          min_desired: 1
  
  min_tasks: 2
  max_tasks: 200
  target_max_message_age_seconds: 120
```

### 4. CloudWatch-based (`cloudwatch`)

Scale based on custom CloudWatch metrics (CPU, memory, custom metrics).

```yaml
autoscaling_configs:
  provider:
    type: cloudwatch
    cloudwatch:
      namespace: AWS/ECS
      metric_name: CPUUtilization
      dimensions:
        ClusterName: production
        ServiceName: my_service
      stat: Average
      period_s: 60
      threshold: 70
      comparison: ">"  # > | >= | < | <= | == | !=
  
  min_tasks: 3
  max_tasks: 50
```

## Global Scaling Parameters

These parameters apply to all provider types:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `min_tasks` | integer | Required | Minimum number of tasks (>= 0) |
| `max_tasks` | integer | Required | Maximum number of tasks (>= 1, >= min_tasks) |
| `target_max_message_age_seconds` | integer | - | Target max age for SQS messages (seconds) |
| `scale_out_cooldown_seconds` | integer | 90 | Cooldown period after scaling out (seconds) |
| `scale_in_cooldown_seconds` | integer | 600 | Cooldown period after scaling in (seconds) |
| `max_scale_out_percent` | integer | 25 | Max % increase per scale-out event (0-100) |
| `max_scale_in_tasks` | integer | 1 | Max number of tasks to remove per scale-in |

## Scale-in Guards

Prevent premature scale-in with guard conditions:

### Empty Queue Mode (Recommended)

Only scale in when the queue is completely empty:

```yaml
scale_in_guard:
  mode: empty_queue
```

### Low Latency Mode

Only scale in when both conditions are met:

```yaml
scale_in_guard:
  mode: low_latency
  age_below_seconds: 20      # Max message age must be below this
  visible_below: 200          # Visible messages must be below this
```

## DynamoDB Schema

The action publishes configurations to table `${ecs_cluster}_ecs_autoscaling_config`:

**Table name:** `${ecs_cluster}_ecs_autoscaling_config` (e.g., `production_ecs_autoscaling_config`, `staging_ecs_autoscaling_config`)

**Primary Key:** `service_key` (String) = `${environment}:${cluster}:${service}`

**Item structure:**
```json
{
  "service_key": "production:prod-cluster:my-service",
  "env": "production",
  "version": 1,
  "config": { /* your autoscaling_configs */ },
  "checksum": "sha256_hex_of_config",
  "commit_sha": "abc123...",
  "updated_at": 1733742000
}
```

**Optimistic concurrency:** Uses condition `attribute_not_exists(service_key) OR updated_at <= :now`

If the condition fails (existing config is newer), the action logs a warning and continues the deployment without failing.

## Action Outputs

The action exposes these outputs for use in subsequent workflow steps:

```yaml
- name: Deploy to ECS
  id: deploy
  uses: delivops/ecs-deploy-action@v0.0.19
  with:
    # ... inputs ...

- name: Check autoscaling publish
  run: |
    echo "Published: ${{ steps.deploy.outputs.autoscaler_published }}"
    echo "Service Key: ${{ steps.deploy.outputs.autoscaler_service_key }}"
    echo "Checksum: ${{ steps.deploy.outputs.autoscaler_checksum }}"
    echo "Updated At: ${{ steps.deploy.outputs.autoscaler_updated_at }}"
```

**Outputs:**
- `autoscaler_published` - `true` or `false`
- `autoscaler_service_key` - DynamoDB primary key (e.g., `production:cluster:service`)
- `autoscaler_checksum` - SHA256 checksum of published config
- `autoscaler_updated_at` - Unix timestamp of publish

## IAM Requirements

The AWS role assumed by the action must have these DynamoDB permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT:table/*_ecs_autoscaling_config"
    }
  ]
}
```

The wildcard pattern `*_ecs_autoscaling_config` allows access to all cluster-specific tables (e.g., `production_ecs_autoscaling_config`, `staging_ecs_autoscaling_config`).

Alternatively, grant access to specific cluster tables:

```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:DescribeTable",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem"
  ],
  "Resource": [
    "arn:aws:dynamodb:REGION:ACCOUNT:table/production_ecs_autoscaling_config",
    "arn:aws:dynamodb:REGION:ACCOUNT:table/staging_ecs_autoscaling_config"
  ]
}
```

## Error Handling

The action is designed to **never fail deployments** due to autoscaling config issues:

- **Config absent:** No action taken, deployment continues
- **Validation fails:** Logs detailed errors, deployment continues
- **DynamoDB publish fails:** Logs error, deployment continues
- **Conditional check fails:** Logs warning (newer config exists), deployment continues

This ensures your code deployments are always prioritized over autoscaling config updates.

## Examples

See the [`examples/`](../examples/) directory for complete examples:

- [`autoscaling-sqs-simple.yaml`](../examples/autoscaling-sqs-simple.yaml) - Basic SQS autoscaling
- [`autoscaling-time-based.yaml`](../examples/autoscaling-time-based.yaml) - Time-based scheduling
- [`autoscaling-sqs-time-combined.yaml`](../examples/autoscaling-sqs-time-combined.yaml) - Combined SQS + Time
- [`autoscaling-low-latency.yaml`](../examples/autoscaling-low-latency.yaml) - Low-latency scale-in guard
- [`autoscaling-time-override-mode.yaml`](../examples/autoscaling-time-override-mode.yaml) - Override mode
- [`autoscaling-cloudwatch.yaml`](../examples/autoscaling-cloudwatch.yaml) - CloudWatch metrics
- [`no-autoscaling.yaml`](../examples/no-autoscaling.yaml) - No autoscaling (default)

## Validation

The action validates your configuration against a JSON schema before publishing. Common validation errors:

- **Invalid SQS URL format:** Must match `https://sqs.REGION.amazonaws.com/ACCOUNT/QUEUE_NAME`
- **Invalid time format:** Must be `HH:MM` (e.g., `09:00`, not `9:00`)
- **max_tasks < min_tasks:** Maximum must be greater than or equal to minimum
- **Override mode missing desired:** When using `mode: override`, at least one rule must have a `desired` field
- **Low latency mode missing fields:** `low_latency` requires both `age_below_seconds` and `visible_below`

## Best Practices

1. **Start simple:** Begin with SQS-only autoscaling, add time-based rules later
2. **Conservative cooldowns:** Use longer `scale_in_cooldown_seconds` (5-10 minutes) to avoid thrashing
3. **Gradual scale-out:** Keep `max_scale_out_percent` reasonable (25-50%) to avoid sudden cost spikes
4. **Use scale-in guards:** Prevent premature scale-in with `empty_queue` or `low_latency` mode
5. **Test in staging:** Validate autoscaling behavior in staging before production
6. **Monitor outputs:** Check `autoscaler_published` output to confirm successful publishes

## Troubleshooting

### Config not publishing?

Check action logs for validation errors:
```
❌ Autoscaling config validation failed:
   Schema validation failed: ...
```

### Conditional check failures?

This is normal if multiple deployments happen concurrently. The action logs:
```
⚠️  Skipped publish for production:cluster:service: 
   existing config is newer (conditional check failed)
```

### Table doesn't exist?

The action will log:
```
Table 'production_ecs_autoscaling_config' does not exist - skipping publish
```

Ensure the DynamoDB table is created for each ECS cluster in your infrastructure (e.g., `production_ecs_autoscaling_config`, `staging_ecs_autoscaling_config`).

## Version History

- **v1** (current) - Initial autoscaling config schema
  - Supports SQS, Time, SQS+Time, CloudWatch providers
  - Optimistic concurrency with `updated_at` field
  - Non-blocking deployment behavior

