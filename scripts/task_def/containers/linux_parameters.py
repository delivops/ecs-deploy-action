import logging
from typing import Any, Dict, Optional

from ..common import ValidationError

logger = logging.getLogger(__name__)


def build_linux_parameters(config: Dict[str, Any], launch_type: str = "FARGATE") -> Optional[Dict[str, Any]]:
    """Build linuxParameters for container definition

    Args:
        config: The YAML configuration dictionary
        launch_type: Launch type (FARGATE or EC2)

    Returns:
        Dict with linuxParameters or None if not configured
    """
    linux_params = config.get("linux_parameters", {})
    if not linux_params:
        return None

    linux_parameters = {}

    # Parameters supported by both Fargate and EC2
    init_process_enabled = linux_params.get("init_process_enabled")
    if init_process_enabled is not None:
        linux_parameters["initProcessEnabled"] = bool(init_process_enabled)
        logger.info(f"Set initProcessEnabled to {bool(init_process_enabled)}")

    # Capabilities (add/drop) - supported by both Fargate and EC2
    capabilities = linux_params.get("capabilities", {})
    if capabilities:
        caps = {}
        if "add" in capabilities and capabilities["add"]:
            caps["add"] = list(capabilities["add"])
        if "drop" in capabilities and capabilities["drop"]:
            caps["drop"] = list(capabilities["drop"])
        if caps:
            linux_parameters["capabilities"] = caps
            logger.info(f"Set capabilities: add={caps.get('add', [])}, drop={caps.get('drop', [])}")

    # tmpfs mounts - supported by both Fargate and EC2
    tmpfs_config = linux_params.get("tmpfs", [])
    if tmpfs_config:
        tmpfs_mounts = []
        for mount in tmpfs_config:
            container_path = mount.get("container_path") or "/tmp"
            raw_size = mount.get("size", 64)
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"Invalid tmpfs size '{raw_size}' for mount {mount!r}. Size must be a positive integer."
                )
            if size <= 0:
                raise ValidationError(
                    f"Invalid tmpfs size '{raw_size}' for mount {mount!r}. Size must be a positive integer greater than zero."
                )
            tmpfs_mount = {
                "containerPath": container_path,
                "size": size,
            }
            mount_options = mount.get("mount_options", [])
            if mount_options:
                tmpfs_mount["mountOptions"] = list(mount_options)
            tmpfs_mounts.append(tmpfs_mount)
        if tmpfs_mounts:
            linux_parameters["tmpfs"] = tmpfs_mounts
            logger.info(f"Set {len(tmpfs_mounts)} tmpfs mounts")

    # swappiness - supported by Fargate (1.4.0+) and EC2
    swappiness = linux_params.get("swappiness")
    if swappiness is not None:
        try:
            swappiness_int = int(swappiness)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"Invalid swappiness value {swappiness!r}; must be an integer between 0 and 100."
            ) from exc
        if not 0 <= swappiness_int <= 100:
            raise ValidationError(
                f"Invalid swappiness value {swappiness_int}; must be between 0 and 100."
            )
        linux_parameters["swappiness"] = swappiness_int
        logger.info(f"Set swappiness to {swappiness_int}")

    # maxSwap - supported by Fargate (1.4.0+) and EC2
    max_swap = linux_params.get("max_swap")
    if max_swap is not None:
        try:
            max_swap_int = int(max_swap)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"Invalid maxSwap value {max_swap!r}; must be a non-negative integer."
            ) from exc
        if max_swap_int < 0:
            raise ValidationError(
                f"Invalid maxSwap value {max_swap_int}; must be a non-negative integer."
            )
        linux_parameters["maxSwap"] = max_swap_int
        logger.info(f"Set maxSwap to {max_swap_int}")

    # EC2-only parameters
    shared_memory_size = linux_params.get("shared_memory_size")
    if shared_memory_size is not None:
        if launch_type == "FARGATE":
            logger.warning("shared_memory_size is EC2-only, ignoring for Fargate launch type")
        else:
            try:
                shared_memory_size_int = int(shared_memory_size)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"Invalid shared_memory_size '{shared_memory_size}': must be a positive integer"
                ) from exc
            if shared_memory_size_int <= 0:
                raise ValidationError(
                    f"Invalid shared_memory_size '{shared_memory_size}': must be a positive integer"
                )
            linux_parameters["sharedMemorySize"] = shared_memory_size_int
            logger.info(f"Set sharedMemorySize to {shared_memory_size_int} MiB")

    # devices - EC2 only (for GPU, etc.)
    devices_config = linux_params.get("devices", [])
    if devices_config:
        if launch_type == "FARGATE":
            logger.warning("devices is EC2-only, ignoring for Fargate launch type")
        else:
            devices = []
            for device in devices_config:
                host_path = device.get("host_path")
                if not host_path:
                    raise ValidationError(
                        "Each entry in linux_parameters.devices must include a non-empty 'host_path'. "
                        f"Invalid device mapping: {device}"
                    )
                container_path = device.get("container_path", host_path)
                permissions = device.get("permissions", ["read", "write"])
                device_mapping = {
                    "hostPath": host_path,
                    "containerPath": container_path,
                    "permissions": permissions,
                }
                devices.append(device_mapping)
            if devices:
                linux_parameters["devices"] = devices
                logger.info(f"Set {len(devices)} device mappings")

    return linux_parameters if linux_parameters else None
