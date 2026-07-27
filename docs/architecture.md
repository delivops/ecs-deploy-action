# CPU, Memory, and Architecture Example

You can specify CPU, memory, and architecture for your ECS service.

## Example YAML

```yaml
cpu: 1024
memory: 2048
cpu_arch: X86_64
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
```

> `role_arn` is optional — when omitted, the task and execution roles are read from SSM.
> See [Task and Execution Roles](./roles.md).
