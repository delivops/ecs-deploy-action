from pathlib import Path
from typing import Any, Dict

import yaml

from .common import ValidationError


def validate_config(config: Dict[str, Any]) -> None:
    """Validate the YAML configuration"""
    # Note: 'name' field is not required since service_name can be used instead
    # No required fields validation for now

    # Get launch type (default: FARGATE for backwards compatibility)
    launch_type = config.get("launch_type", "FARGATE").upper()

    # Validate launch_type
    valid_launch_types = ["FARGATE", "EC2"]
    if launch_type not in valid_launch_types:
        raise ValidationError(f"Invalid launch_type: {launch_type}. Must be one of {valid_launch_types}")

    # Validate network_mode for EC2 (Fargate only supports awsvpc)
    network_mode = config.get("network_mode", "awsvpc").lower()
    valid_network_modes = ["awsvpc", "bridge", "host", "none"]
    if network_mode not in valid_network_modes:
        raise ValidationError(f"Invalid network_mode: {network_mode}. Must be one of {valid_network_modes}")

    if launch_type == "FARGATE" and network_mode != "awsvpc":
        raise ValidationError(f"Fargate only supports 'awsvpc' network mode, got: {network_mode}")

    # Validate CPU and memory values
    cpu = config.get("cpu", 256)
    memory = config.get("memory", 512)

    if launch_type == "FARGATE":
        # Fargate has strict CPU/memory requirements
        valid_cpu_values = [256, 512, 1024, 2048, 4096]
        if cpu not in valid_cpu_values:
            raise ValidationError(f"Invalid CPU value: {cpu}. Must be one of {valid_cpu_values}")

        # Validate memory based on CPU
        valid_memory_for_cpu = {
            256: [512, 1024, 2048],
            512: [1024, 2048, 3072, 4096],
            1024: [2048, 3072, 4096, 5120, 6144, 7168, 8192],
            2048: list(range(4096, 16385, 1024)),
            4096: list(range(8192, 30721, 1024)),
        }

        if memory not in valid_memory_for_cpu.get(cpu, []):
            raise ValidationError(f"Invalid memory value {memory} for CPU {cpu}")
    else:
        # EC2 has more flexible CPU/memory - just validate they're positive if provided
        if cpu is not None and (not isinstance(cpu, int) or cpu <= 0):
            raise ValidationError(f"Invalid CPU value: {cpu}. Must be a positive integer.")
        if memory is not None and (not isinstance(memory, int) or memory <= 0):
            raise ValidationError(f"Invalid memory value: {memory}. Must be a positive integer.")

    # Validate secrets_envs structure and new parsing options
    secrets_envs = config.get("secrets_envs", [])
    if secrets_envs is None:
        secrets_envs = []

    if not isinstance(secrets_envs, list):
        raise ValidationError("Invalid secrets_envs: must be a list of secret configurations")

    for idx, secret_config in enumerate(secrets_envs):
        if not isinstance(secret_config, dict):
            raise ValidationError(f"Invalid secrets_envs[{idx}]: each item must be a mapping/object")

        auto_parse_keys_to_envs = secret_config.get("auto_parse_keys_to_envs", True)
        if not isinstance(auto_parse_keys_to_envs, bool):
            raise ValidationError(
                f"Invalid secrets_envs[{idx}].auto_parse_keys_to_envs: must be a boolean"
            )

        secret_id = secret_config.get("id")
        if secret_id is not None and not isinstance(secret_id, str):
            raise ValidationError(f"Invalid secrets_envs[{idx}].id: must be a string")

        secret_name = secret_config.get("name")
        if secret_name is not None and not isinstance(secret_name, str):
            raise ValidationError(f"Invalid secrets_envs[{idx}].name: must be a string")

        if not auto_parse_keys_to_envs:
            env_name = secret_config.get("env_name", "")
            if not isinstance(env_name, str) or not env_name.strip():
                raise ValidationError(
                    f"Invalid secrets_envs[{idx}]: env_name is required when auto_parse_keys_to_envs is false"
                )

            has_id = bool(secret_config.get("id", "").strip())
            has_name = bool(secret_config.get("name", "").strip())
            if not has_id and not has_name:
                raise ValidationError(
                    f"Invalid secrets_envs[{idx}]: either id or name is required when auto_parse_keys_to_envs is false"
                )

        values = secret_config.get("values")
        if values is not None and not isinstance(values, list):
            raise ValidationError(f"Invalid secrets_envs[{idx}].values: must be a list")

        if isinstance(values, list):
            for value_idx, value in enumerate(values):
                if not isinstance(value, str) or not value.strip():
                    raise ValidationError(
                        f"Invalid secrets_envs[{idx}].values[{value_idx}]: must be a non-empty string"
                    )


def load_and_validate_config(yaml_file_path: str) -> Dict[str, Any]:
    """Load and validate YAML configuration"""
    try:
        yaml_path = Path(yaml_file_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")

        with yaml_path.open("r") as file:
            config = yaml.safe_load(file)

        if not config:
            raise ValidationError("YAML file is empty or invalid")

        validate_config(config)
        return config

    except yaml.YAMLError as e:
        raise ValidationError(f"Invalid YAML format: {e}")
