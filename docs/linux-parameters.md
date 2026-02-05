# Linux Parameters

Configure Linux-specific settings for your container using the `linux_parameters` block.

## Configuration

```yaml
linux_parameters:
  init_process_enabled: true
  capabilities:
    add:
      - SYS_PTRACE
    drop:
      - NET_RAW
  tmpfs:
    - container_path: /tmp
      size: 128
      mount_options:
        - noexec
        - nosuid
  swappiness: 60
  max_swap: 0
  # EC2 only:
  shared_memory_size: 256
  devices:
    - host_path: /dev/nvidia0
      container_path: /dev/nvidia0
      permissions:
        - read
        - write
        - mknod
```

## Available Parameters

### Both Fargate and EC2

| Parameter | Type | Description |
|-----------|------|-------------|
| `init_process_enabled` | bool | Run an init process (PID 1) inside the container. Useful for proper signal handling and zombie process cleanup. |
| `capabilities.add` | list | Linux capabilities to add to the container. |
| `capabilities.drop` | list | Linux capabilities to drop from the container. |
| `tmpfs` | list | Mount tmpfs (in-memory) filesystems in the container. |
| `swappiness` | int | Memory swappiness value (0-100). Requires Fargate platform 1.4.0+. |
| `max_swap` | int | Maximum amount of swap (in MiB) the container can use. Requires Fargate platform 1.4.0+. |

### EC2 Only

These parameters are only supported when `launch_type: EC2`. If used with Fargate, a warning is logged and the parameter is ignored.

| Parameter | Type | Description |
|-----------|------|-------------|
| `shared_memory_size` | int | Size (in MiB) of the `/dev/shm` shared memory volume. |
| `devices` | list | Host device mappings to the container (for GPU access, etc.). |

## Parameter Details

### init_process_enabled

When set to `true`, an init process runs as PID 1 inside the container. This helps with:
- Proper signal forwarding to child processes
- Cleaning up zombie processes
- Graceful shutdown handling

```yaml
linux_parameters:
  init_process_enabled: true
```

### capabilities

Add or drop Linux capabilities for fine-grained permission control:

```yaml
linux_parameters:
  capabilities:
    add:
      - SYS_PTRACE      # Enable debugging with ptrace
      - NET_ADMIN       # Network administration
    drop:
      - NET_RAW         # Drop raw socket access
      - MKNOD           # Drop device node creation
```

Common capabilities:
- `SYS_PTRACE`: Required for debugging tools, profilers
- `NET_ADMIN`: Network configuration
- `NET_RAW`: Raw and packet sockets
- `IPC_LOCK`: Lock memory

### tmpfs

Mount temporary filesystems in memory:

```yaml
linux_parameters:
  tmpfs:
    - container_path: /tmp
      size: 128          # Size in MiB
      mount_options:
        - noexec         # Prevent execution
        - nosuid         # Ignore setuid bits
        - nodev          # No device files
    - container_path: /run
      size: 64
```

### shared_memory_size (EC2 only)

Set the size of `/dev/shm` for inter-process communication:

```yaml
launch_type: EC2
linux_parameters:
  shared_memory_size: 256  # Size in MiB
```

Common use cases:
- Machine learning workloads
- Chrome/Puppeteer (requires larger shm)
- Database shared buffers
- Inter-process communication

### devices (EC2 only)

Map host devices to the container (e.g., for GPU access):

```yaml
launch_type: EC2
linux_parameters:
  devices:
    - host_path: /dev/nvidia0
      container_path: /dev/nvidia0
      permissions:
        - read
        - write
        - mknod
```

## Examples

### Fargate with Init Process

```yaml
name: my-fargate-app
cpu: 512
memory: 1024
launch_type: FARGATE
linux_parameters:
  init_process_enabled: true
  capabilities:
    drop:
      - NET_RAW
```

### EC2 with GPU Access

```yaml
name: gpu-workload
cpu: 4096
memory: 16384
launch_type: EC2
linux_parameters:
  init_process_enabled: true
  shared_memory_size: 2048
  devices:
    - host_path: /dev/nvidia0
      container_path: /dev/nvidia0
      permissions:
        - read
        - write
        - mknod
```

### EC2 for Machine Learning

```yaml
name: ml-training
cpu: 4096
memory: 30720
launch_type: EC2
linux_parameters:
  shared_memory_size: 4096  # Large shm for PyTorch DataLoader
  init_process_enabled: true
  tmpfs:
    - container_path: /tmp
      size: 1024
```

## See Also

- [EC2 Launch Type](ec2-launch-type.md) - Configure EC2 vs Fargate
- [Basic Configuration](basic.md) - Core configuration options
