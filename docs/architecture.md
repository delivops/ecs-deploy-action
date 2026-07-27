# CPU, Memory, and Architecture

You can specify CPU, memory, and CPU architecture for your task.

## Example YAML

```yaml
cpu: 1024
memory: 2048
cpu_arch: X86_64
role_arn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
```

Defaults are `cpu: 256` and `memory: 512` when omitted.

## CPU architecture

| Value | Notes |
|---|---|
| `X86_64` | (Default) Intel/AMD 64-bit |
| `ARM64` | AWS Graviton |

`cpu_arch` is emitted inside `runtimePlatform`, which is only included for Fargate tasks. It has no
effect under `launch_type: EC2` — the instance's own architecture applies there.

## Fargate CPU/memory combinations

Fargate only accepts fixed pairings, and an invalid pair fails validation before anything is
registered:

| CPU | Valid memory (MiB) |
|---|---|
| 256 | 512, 1024, 2048 |
| 512 | 1024, 2048, 3072, 4096 |
| 1024 | 2048 – 8192, in 1024 steps |
| 2048 | 4096 – 16384, in 1024 steps |
| 4096 | 8192 – 30720, in 1024 steps |
| 8192 | 16384 – 61440, in 4096 steps |
| 16384 | 32768 – 122880, in 8192 steps |

## EC2

Under `launch_type: EC2`, `cpu` and `memory` only need to be positive integers — the fixed Fargate
tiers do not apply. See [EC2 Launch Type](ec2-launch-type.md).

## Related options

| Option | Description |
|---|---|
| `ephemeral_storage` | Task ephemeral storage in GiB. AWS default is 20 GiB. See [`examples/ephemeral-storage.yaml`](../examples/ephemeral-storage.yaml). |
| `replica_count` | Service desired count on every deploy. See [Basic Usage](basic.md). |
