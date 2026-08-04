#!/usr/bin/env python3
import yaml
import json
import argparse
import os
import re
import sys
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
from enum import Enum

class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"

class ValidationError(Exception):
    """Custom exception for validation errors"""
    pass

class RoleResolutionError(Exception):
    """Raised when taskRoleArn / executionRoleArn cannot be resolved.

    Deliberately not a subclass of ValidationError: the config may be perfectly
    valid and the failure be environmental (missing SSM parameter, no
    credentials), so it must not be reported as "validation failed".
    """
    pass

# YAML keys that hold an IAM role ARN, in no particular order.
ROLE_ARN_KEYS = ('role_arn', 'task_role_arn', 'execution_role_arn')

# arn:<partition>:iam::<12-digit account>:role/<path and name, no whitespace>
ROLE_ARN_PATTERN = re.compile(r'^arn:aws[a-z0-9-]*:iam::\d{12}:role/\S+$')

def setup_logging(level: str = "INFO") -> logging.Logger:
    """Setup logging configuration"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stderr)  # Send logs to stderr instead of stdout
        ]
    )
    return logging.getLogger(__name__)

# Initialize logger
logger = setup_logging()

def normalize_role_value(config: Dict[str, Any], key: str,
                         error_cls: type = ValidationError) -> Optional[str]:
    """Normalize a role YAML value to a stripped string, or None when unset.

    A missing key, YAML null and the empty string all mean "not set here" and
    fall through to the next precedence level.
    """
    raw = config.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        # YAML 1.1 resolves bare no/off/yes/on to booleans, so an unquoted value
        # can arrive here as False and produce a baffling downstream error.
        raise error_cls(
            f"{key} was read as the boolean {raw}. YAML treats bare no/off/yes/on "
            f"as booleans - quote the value, and set it to a full IAM role ARN."
        )
    value = str(raw).strip()
    return value or None

def validate_role_arn(value: str, source: str,
                      error_cls: type = ValidationError) -> None:
    """Reject anything that is not a full IAM role ARN.

    The Terraform module validates the same thing on its side ("must be a full
    IAM role ARN, not a role name"), so a bare name here is a real mistake.
    """
    if not ROLE_ARN_PATTERN.match(value):
        raise error_cls(
            f"{source} is not an IAM role ARN: '{value}'. Expected "
            f"arn:aws:iam::<account-id>:role/<name>."
        )

def validate_config(config: Dict[str, Any]) -> None:
    """Validate the YAML configuration"""
    # Note: 'name' field is not required since service_name can be used instead
    # No required fields validation for now
    
    # Get launch type (default: FARGATE for backwards compatibility)
    launch_type = config.get('launch_type', 'FARGATE').upper()
    
    # Validate launch_type
    valid_launch_types = ['FARGATE', 'EC2']
    if launch_type not in valid_launch_types:
        raise ValidationError(f"Invalid launch_type: {launch_type}. Must be one of {valid_launch_types}")
    
    # Validate network_mode for EC2 (Fargate only supports awsvpc)
    network_mode = config.get('network_mode', 'awsvpc').lower()
    valid_network_modes = ['awsvpc', 'bridge', 'host', 'none']
    if network_mode not in valid_network_modes:
        raise ValidationError(f"Invalid network_mode: {network_mode}. Must be one of {valid_network_modes}")
    
    if launch_type == 'FARGATE' and network_mode != 'awsvpc':
        raise ValidationError(f"Fargate only supports 'awsvpc' network mode, got: {network_mode}")
    
    # Validate CPU and memory values
    cpu = config.get('cpu', 256)
    memory = config.get('memory', 512)
    
    if launch_type == 'FARGATE':
        # Fargate has strict CPU/memory requirements
        valid_cpu_values = [256, 512, 1024, 2048, 4096, 8192, 16384]
        if cpu not in valid_cpu_values:
            raise ValidationError(
                f"Invalid CPU value: {cpu}. Must be one of {valid_cpu_values}"
            )
        
        # Validate memory based on CPU
        valid_memory_for_cpu = {
            256: [512, 1024, 2048],
            512: [1024, 2048, 3072, 4096],
            1024: [2048, 3072, 4096, 5120, 6144, 7168, 8192],
            2048: list(range(4096, 16385, 1024)),    # 4 GB to 16 GB
            4096: list(range(8192, 30721, 1024)),    # 8 GB to 30 GB
            8192: list(range(16384, 61441, 4096)),   # 16 GB to 60 GB
            16384: list(range(32768, 122881, 8192)), # 32 GB to 120 GB
        }
        
        if memory not in valid_memory_for_cpu.get(cpu, []):
            raise ValidationError(
                f"Invalid memory value {memory} for CPU {cpu}. "
                f"Valid values are: {valid_memory_for_cpu.get(cpu, [])}"
            )
    else:
        # EC2 has more flexible CPU/memory - just validate they're positive if provided
        if cpu is not None and (not isinstance(cpu, int) or cpu <= 0):
            raise ValidationError(f"Invalid CPU value: {cpu}. Must be a positive integer.")
        if memory is not None and (not isinstance(memory, int) or memory <= 0):
            raise ValidationError(f"Invalid memory value: {memory}. Must be a positive integer.")

    # Role ARN shape. Actual resolution (including the SSM fallback) happens
    # later in RoleResolver; checking the shape here keeps --validate-only
    # entirely offline while still catching typos and bare role names.
    for key in ROLE_ARN_KEYS:
        value = normalize_role_value(config, key)
        if value is None:
            continue
        validate_role_arn(value, f"YAML key '{key}'")

    validate_sidecars(config)

# Fields that are arrays and should be extended (appended) during merge
ARRAY_FIELDS = {
    'command', 'entrypoint', 'envs', 'envs_from_files', 'secrets', 'secrets_envs',
    'secret_files', 'additional_ports', 'writable_dirs'
}

# Fields that are objects and use shallow merge (replace)
OBJECT_FIELDS = {
    'health_check', 'linux_parameters', 'otel_collector', 'fluent_bit_collector'
}

def merge_sidecars(base_sidecars: Any, override_sidecars: Any) -> Any:
    """Merge two `sidecars` lists by container name rather than appending.

    Blindly extending (the rule for every other array field) would produce two
    containers with the same name, which ECS rejects. Instead a service override
    that names an existing sidecar patches it - recursively, so the array/object/
    null rules of merge_configs apply inside the sidecar too - and a name not in
    the base is appended. Base order is preserved.
    """
    if not isinstance(base_sidecars, list) or not isinstance(override_sidecars, list):
        # Malformed input: let validate_sidecars report it with a good message.
        return override_sidecars

    merged = [dict(s) if isinstance(s, dict) else s for s in base_sidecars]
    index = {
        s['name']: i for i, s in enumerate(merged)
        if isinstance(s, dict) and isinstance(s.get('name'), str)
    }

    for override in override_sidecars:
        if not isinstance(override, dict):
            merged.append(override)
            continue
        name = override.get('name')
        position = index.get(name) if isinstance(name, str) else None
        if position is None:
            merged.append(dict(override))
            if isinstance(name, str):
                index[name] = len(merged) - 1
            continue
        merged[position] = merge_configs(merged[position], override)

    return merged

def merge_configs(base_config: Dict[str, Any], service_override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge base configuration with service-specific overrides.

    - Scalars: Override replaces base
    - Arrays: Service values appended to base (extend)
    - Objects: Service object completely replaces base (shallow merge)
    - sidecars: Merged by name (see merge_sidecars)
    """
    # Start with a copy of base config (excluding services_overrides)
    merged = {k: v for k, v in base_config.items() if k != 'services_overrides'}

    for key, override_value in service_override.items():
        if override_value is None:
            # Explicit null removes the field
            merged.pop(key, None)
            continue

        if key == 'sidecars':
            merged[key] = merge_sidecars(merged.get(key, []), override_value)
        elif key in ARRAY_FIELDS:
            # Extend: append service array to base array
            base_array = merged.get(key, [])
            if isinstance(base_array, list) and isinstance(override_value, list):
                merged[key] = base_array + override_value
            else:
                merged[key] = override_value
        else:
            # Scalars and objects: override replaces base
            merged[key] = override_value

    return merged

def apply_service_overrides(raw_config: Dict[str, Any], service_name: Optional[str]) -> Dict[str, Any]:
    """
    Apply service-specific overrides to the base configuration.

    If services_overrides exists and contains the service_name,
    merge those overrides with the base config.
    """
    services_overrides = raw_config.get('services_overrides', {})

    if not services_overrides:
        # No overrides section - return config without services_overrides key
        return {k: v for k, v in raw_config.items() if k != 'services_overrides'}

    if not service_name:
        logger.warning("services_overrides present but no service_name provided")
        return {k: v for k, v in raw_config.items() if k != 'services_overrides'}

    service_override = services_overrides.get(service_name, {})

    if not service_override:
        logger.info(f"No overrides found for service '{service_name}', using base config")
        return {k: v for k, v in raw_config.items() if k != 'services_overrides'}

    logger.info(f"Applying overrides for service '{service_name}': {list(service_override.keys())}")
    return merge_configs(raw_config, service_override)

def validate_services_overrides(config: Dict[str, Any]) -> None:
    """Validate the services_overrides section if present"""
    services_overrides = config.get('services_overrides')

    if services_overrides is None:
        return  # No overrides, nothing to validate

    if not isinstance(services_overrides, dict):
        raise ValidationError(
            f"services_overrides must be a dictionary, got {type(services_overrides).__name__}"
        )

    for svc_name, overrides in services_overrides.items():
        if not isinstance(svc_name, str):
            raise ValidationError(
                f"Service name in services_overrides must be a string, got {type(svc_name).__name__}"
            )

        if overrides is not None and not isinstance(overrides, dict):
            raise ValidationError(
                f"Overrides for service '{svc_name}' must be a dictionary, "
                f"got {type(overrides).__name__}"
            )

# ---------------------------------------------------------------------------
# Generic sidecars
# ---------------------------------------------------------------------------

# ECS container and volume names share this shape: up to 255 letters, numbers,
# hyphens and underscores. Enforced here so a bad sidecar name fails with a
# clear message instead of an opaque RegisterTaskDefinition rejection.
ECS_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,254}$')

# Port mapping names are stricter than container names: lowercase only, and
# capped at 64 characters.
ECS_PORT_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')

# Container names this generator produces itself. A sidecar may not take one.
# 'default' is reserved too: build_log_configuration rewrites that stream prefix
# to '/default' for backwards compatibility, so a sidecar named 'default' would
# silently log into the application's stream.
RESERVED_CONTAINER_NAMES = frozenset({
    'app', 'init-container-for-secret-files', 'fluent-bit', 'otel-collector', 'default',
})

def _is_positive_int(value: Any) -> bool:
    # bool is an int subclass in Python, so `port: true` must not pass as a port.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

# Every key a sidecar block may set, mapped to its type rule. This table is the
# single source of truth: the allowed-key set is derived from it below, so a new
# key cannot be accepted without also being type-checked, or type-checked while
# still being rejected as unsupported.
SIDECAR_KEY_TYPES: Dict[str, tuple] = {
    key: (lambda v: isinstance(v, list), "a list")
    for key in ('command', 'entrypoint', 'envs', 'envs_from_files', 'secrets',
                'secrets_envs', 'secret_files', 'writable_dirs', 'additional_ports')
}
SIDECAR_KEY_TYPES.update({
    key: (lambda v: isinstance(v, dict), "a mapping")
    for key in ('health_check', 'linux_parameters')
})
SIDECAR_KEY_TYPES.update({
    key: (lambda v: isinstance(v, str), "a string")
    for key in ('name', 'image', 'secrets_files_path', 'app_protocol',
                'log_stream_prefix')
})
SIDECAR_KEY_TYPES.update({
    key: (lambda v: isinstance(v, bool), "true or false")
    for key in ('enabled', 'essential', 'readonly_root_filesystem')
})
SIDECAR_KEY_TYPES.update({
    key: (_is_positive_int, "a positive integer")
    for key in ('port', 'cpu', 'memory', 'memory_reservation', 'stop_timeout')
})

# Anything outside the table is a typo and is rejected - silently ignoring an
# unknown key is how a sidecar ends up missing the secret or mount its author
# thought they had configured.
SIDECAR_ALLOWED_KEYS = frozenset(SIDECAR_KEY_TYPES)

def volume_name_for_writable_dir(dir_path: str, prefix: str = "") -> str:
    """Generate the volume name backing a writable directory.

    `/var/run` becomes `writable-var-run`, or `<prefix>-writable-var-run` for a
    sidecar. The prefix is what keeps two sidecars that both want /tmp on two
    distinct volumes.
    """
    # Deliberately not str()-coerced: a non-string path raises here exactly as it
    # did before this helper existed, rather than silently producing a volume
    # named after an integer and a containerPath ECS will reject.
    suffix = "writable-" + dir_path.strip("/").replace("/", "-")
    return f"{prefix}-{suffix}" if prefix else suffix

def sidecar_init_container_name(sidecar_name: str) -> str:
    """Name of the secret-file init container generated for a sidecar."""
    return f"{sidecar_name}-secret-init"

def sidecar_secrets_volume_name(sidecar_name: str) -> str:
    """Name of the volume carrying a sidecar's downloaded secret files."""
    return f"{sidecar_name}-secrets"

def sidecar_is_enabled(sidecar: Dict[str, Any]) -> bool:
    """Whether a sidecar block should be rendered.

    Only an explicit `enabled: false` switches one off. A missing key and a YAML
    null both mean enabled - `enabled:` with nothing after it parses as None,
    and silently dropping a container for that would be a nasty surprise.
    """
    return sidecar.get('enabled') is not False

def enabled_sidecars(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The sidecar blocks that should actually be rendered, in declaration order."""
    return [s for s in (config.get('sidecars') or []) if sidecar_is_enabled(s)]

def _validate_sidecar_types(sidecar: Dict[str, Any], label: str) -> None:
    """Type-check the supported keys of one sidecar block."""
    for key, value in sidecar.items():
        if value is None:
            continue  # An explicit null means "unset"; treated as absent.
        rule = SIDECAR_KEY_TYPES.get(key)
        if rule is None:
            continue  # Unknown keys are already rejected by the caller.
        matches, expected = rule
        if not matches(value):
            # A wrong-typed value is usually best described by its type, but for
            # a rejected number the value itself is what the author needs to see.
            got = repr(value) if expected == "a positive integer" else type(value).__name__
            raise ValidationError(f"{label}: '{key}' must be {expected}, got {got}")

    memory = sidecar.get('memory')
    reservation = sidecar.get('memory_reservation')
    if memory is not None and reservation is not None and reservation > memory:
        raise ValidationError(
            f"{label}: memory_reservation ({reservation}) must not exceed "
            f"memory ({memory}) - ECS rejects a soft limit above the hard limit."
        )

def validate_sidecars(config: Dict[str, Any]) -> None:
    """Validate the `sidecars` block.

    Pure and idempotent: called once early (before anything iterates sidecars,
    so a malformed block fails with a real message rather than an AttributeError)
    and again from validate_config, so callers that build a config dict by hand
    are validated too.
    """
    sidecars = config.get('sidecars')
    if sidecars is None:
        return

    if not isinstance(sidecars, list):
        raise ValidationError(
            f"sidecars must be a list of mappings, got {type(sidecars).__name__}"
        )

    # Names the finished task definition will contain, so a declared name that
    # collides with a *generated* one is caught too.
    container_names: Dict[str, str] = {}
    for name in RESERVED_CONTAINER_NAMES:
        container_names[name] = "reserved by the action"

    volume_names: Dict[str, str] = {}
    for index, sidecar in enumerate(sidecars):
        label = f"sidecars[{index}]"
        if not isinstance(sidecar, dict):
            raise ValidationError(
                f"{label} must be a mapping, got {type(sidecar).__name__}"
            )

        unknown = sorted(set(sidecar) - SIDECAR_ALLOWED_KEYS)
        if unknown:
            raise ValidationError(
                f"{label}: unsupported key(s) {unknown}. Supported keys: "
                f"{sorted(SIDECAR_ALLOWED_KEYS)}"
            )

        name = sidecar.get('name')
        if not name or not isinstance(name, str):
            raise ValidationError(f"{label}: 'name' is required and must be a string")
        label = f"sidecars[{index}] ('{name}')"

        if not ECS_NAME_PATTERN.match(name):
            raise ValidationError(
                f"{label}: invalid container name. Must start with a letter or digit "
                f"and contain only letters, digits, '-' and '_' (max 255 characters)."
            )

        image = sidecar.get('image')
        if not image or not isinstance(image, str):
            raise ValidationError(f"{label}: 'image' is required and must be a string")

        _validate_sidecar_types(sidecar, label)

        for entry in sidecar.get('envs') or []:
            if not isinstance(entry, dict):
                raise ValidationError(
                    f"{label}: envs entries must be single-key mappings of name to "
                    f"value, got {entry!r}"
                )

        # `default` is what build_log_configuration rewrites to the application's
        # '/default' stream, so allowing it here would reopen the collision the
        # reserved container name closes.
        if sidecar.get('log_stream_prefix') == 'default':
            raise ValidationError(
                f"{label}: log_stream_prefix 'default' is reserved for the "
                f"application container's log stream."
            )

        # A disabled sidecar still has to be well-formed - it is a base-config
        # entry a service switched off, and it will be switched back on one day -
        # but it contributes no names to the task definition.
        if not sidecar_is_enabled(sidecar):
            continue

        generated = [(name, f"declared by {label}")]
        if sidecar.get('secret_files'):
            generated.append((
                sidecar_init_container_name(name),
                f"init container generated for {label}",
            ))
        for candidate, origin in generated:
            if not ECS_NAME_PATTERN.match(candidate):
                raise ValidationError(
                    f"{label}: generated container name '{candidate}' ({origin}) is "
                    f"not a valid ECS container name - the sidecar name is too long."
                )
            clash = container_names.get(candidate)
            if clash:
                raise ValidationError(
                    f"Duplicate container name '{candidate}': {origin} but it is "
                    f"already {clash}."
                )
            container_names[candidate] = origin

        candidates = []
        if sidecar.get('secret_files'):
            candidates.append((sidecar_secrets_volume_name(name), 'secret_files'))
        for dir_path in sidecar.get('writable_dirs') or []:
            if not isinstance(dir_path, str) or not dir_path.strip('/'):
                raise ValidationError(
                    f"{label}: writable_dirs entries must be non-empty absolute "
                    f"paths, got {dir_path!r}"
                )
            candidates.append((volume_name_for_writable_dir(dir_path, name), dir_path))

        for volume, origin in candidates:
            if not ECS_NAME_PATTERN.match(volume):
                raise ValidationError(
                    f"{label}: generated volume name '{volume}' (from {origin}) is not "
                    f"a valid ECS volume name. Use only letters, digits, '-' and '_' "
                    f"in the sidecar name and directory path."
                )
            clash = volume_names.get(volume)
            if clash:
                raise ValidationError(
                    f"{label}: generated volume name '{volume}' (from {origin}) "
                    f"collides with the volume generated from {clash}."
                )
            volume_names[volume] = f"{label} {origin}"

    # Sidecar volumes must not collide with the application's own volumes.
    app_volumes = {'shared-volume'} if config.get('secret_files') else set()
    app_volumes.update(
        volume_name_for_writable_dir(d) for d in (config.get('writable_dirs') or [])
        if isinstance(d, str)
    )
    for volume in sorted(volume_names.keys() & app_volumes):
        raise ValidationError(
            f"Generated volume name '{volume}' from {volume_names[volume]} collides "
            f"with an application-level volume of the same name."
        )

    # Port mapping names are unique per task definition, not per container.
    _validate_port_mapping_names(config, sidecars)

    _validate_sidecar_resource_budget(config, sidecars)

def _validate_sidecar_resource_budget(config: Dict[str, Any],
                                      sidecars: List[Dict[str, Any]]) -> None:
    """Keep container-level reservations inside the task's Fargate budget.

    On Fargate, ECS rejects a task whose containers reserve more CPU or memory
    than the task itself. Catching it here turns a mid-deploy
    RegisterTaskDefinition failure into a config error. EC2 task-level values are
    advisory, so the check does not apply there.
    """
    if config.get('launch_type', 'FARGATE').upper() != 'FARGATE':
        return

    enabled = [s for s in sidecars if isinstance(s, dict) and sidecar_is_enabled(s)]

    for yaml_key, task_default, unit in (('cpu', 256, 'CPU units'),
                                         ('memory', 512, 'MiB')):
        task_total = config.get(yaml_key, task_default)
        if not isinstance(task_total, int):
            continue  # validate_config reports a bad task-level value.

        claimed = [(s['name'], s[yaml_key]) for s in enabled
                   if isinstance(s.get(yaml_key), int)]
        reserved = sum(value for _, value in claimed)
        if not claimed or reserved < task_total:
            continue

        breakdown = ', '.join(f"{name}={value}" for name, value in claimed)
        raise ValidationError(
            f"Sidecars reserve {reserved} {unit} ({breakdown}) but the task only "
            f"has {task_total}. Container-level {yaml_key} must leave room for the "
            f"application container - raise the task-level '{yaml_key}' or lower "
            f"the sidecar reservations."
        )

def _sidecar_main_port_name(sidecar_name: str, port: int) -> str:
    """Port-mapping name for a sidecar's primary port.

    Mirrors the `otel-collector-4317-tcp` convention already used for the OTEL
    container. It cannot be "default" - that name is taken by the application's
    port and ECS requires port-mapping names to be unique across the whole task.
    """
    return f"{sidecar_name}-{port}-tcp"

def _validate_port_mapping_names(config: Dict[str, Any], sidecars: List[Dict[str, Any]]) -> None:
    """Reject port mapping names and container ports a sidecar would duplicate.

    Both namespaces are scoped to the whole task definition rather than to one
    container, so a sidecar can collide with the application, with the OTEL
    collector, or with another sidecar. Only collisions *involving a sidecar*
    are reported: a config whose own additional_ports already repeat a name
    predates this feature and is not this change's business to start failing.
    """
    seen: Dict[str, str] = {}
    # Under awsvpc and host every container shares one network namespace, so two
    # containers cannot listen on the same port. Under bridge, hostPort 0 lets
    # Docker assign a free one, so duplicates are fine.
    exclusive_ports = config.get('network_mode', 'awsvpc').lower() in ('awsvpc', 'host')
    ports_seen: Dict[int, str] = {}

    def record(port_name: Any, port: Any, origin: str) -> None:
        seen.setdefault(str(port_name), origin)
        if isinstance(port, int):
            ports_seen.setdefault(port, origin)

    def claim(port_name: Any, port: Any, origin: str) -> None:
        key = str(port_name)
        clash = seen.get(key)
        if clash:
            raise ValidationError(
                f"Duplicate port mapping name '{key}': used by {origin} and by "
                f"{clash}. ECS requires port mapping names to be unique across the "
                f"whole task definition."
            )
        if not ECS_PORT_NAME_PATTERN.match(key):
            raise ValidationError(
                f"Invalid port mapping name '{key}' from {origin}. ECS port mapping "
                f"names must start with a lowercase letter and contain only "
                f"lowercase letters, digits, '-' and '_' (max 64 characters)."
            )
        seen[key] = origin

        if not isinstance(port, int) or not exclusive_ports:
            return
        port_clash = ports_seen.get(port)
        if port_clash:
            raise ValidationError(
                f"Duplicate container port {port}: used by {origin} and by "
                f"{port_clash}. With network_mode "
                f"'{config.get('network_mode', 'awsvpc')}' every container shares one "
                f"network interface, so two containers cannot listen on the same port."
            )
        ports_seen[port] = origin

    if config.get('port'):
        record('default', config.get('port'), "the application's 'port'")
    for entry in config.get('additional_ports') or []:
        if isinstance(entry, dict):
            for port_name, port in entry.items():
                record(port_name, port, "the application's 'additional_ports'")

    # The OTEL collector hardcodes these two mappings when it is enabled.
    if config.get('otel_collector') is not None:
        record('otel-collector-4317-tcp', 4317, "the otel-collector")
        record('otel-collector-4318-tcp', 4318, "the otel-collector")

    for sidecar in sidecars:
        if not isinstance(sidecar, dict) or not sidecar_is_enabled(sidecar):
            continue
        name = sidecar.get('name')
        port = sidecar.get('port')
        if port:
            claim(_sidecar_main_port_name(name, port), port, f"sidecar '{name}' 'port'")
        for entry in sidecar.get('additional_ports') or []:
            if not isinstance(entry, dict):
                raise ValidationError(
                    f"sidecar '{name}': additional_ports entries must be mappings of "
                    f"name to port, got {entry!r}"
                )
            for port_name, entry_port in entry.items():
                if not _is_positive_int(entry_port):
                    raise ValidationError(
                        f"sidecar '{name}': additional_ports port for '{port_name}' "
                        f"must be a positive integer, got {entry_port!r}"
                    )
                claim(port_name, entry_port, f"sidecar '{name}' 'additional_ports'")

DOTENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def parse_dotenv_file(path: Path) -> Dict[str, str]:
    """Parse a strict-minimal dotenv file into an ordered dict.

    Supported: KEY=value, blank lines, full-line `#` comments, surrounding
    matched single/double quotes stripped. Anything else raises ValidationError.
    Last occurrence of a key wins.
    """
    if not path.exists():
        raise ValidationError(f"envs_from_files: file not found: {path}")

    result: Dict[str, str] = {}
    with path.open('r') as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.rstrip('\n').rstrip('\r')
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if '=' not in line:
                raise ValidationError(f"{path}:{lineno}: invalid syntax (missing '=')")
            key, value = line.split('=', 1)
            key = key.strip()
            if not DOTENV_KEY_RE.match(key):
                raise ValidationError(f"{path}:{lineno}: invalid key {key!r}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result

def expand_envs_from_files(config: Dict[str, Any], yaml_path: Path,
                           label: str = "the task config") -> None:
    """Resolve envs_from_files paths relative to the YAML, then merge entries
    into config['envs']. File-derived values are overridden by inline envs;
    later files in the list override earlier ones. Mutates config in place.

    Also used per sidecar, hence `label` - it only distinguishes the log line.
    """
    file_refs = config.pop('envs_from_files', None)
    if not file_refs:
        return

    if not isinstance(file_refs, list):
        raise ValidationError(
            f"envs_from_files must be a list, got {type(file_refs).__name__}"
        )

    yaml_dir = yaml_path.parent
    merged: Dict[str, str] = {}
    for ref in file_refs:
        if not isinstance(ref, str):
            raise ValidationError(
                f"envs_from_files entries must be strings, got {type(ref).__name__}"
            )
        ref_path = Path(ref)
        if not ref_path.is_absolute():
            ref_path = (yaml_dir / ref_path).resolve()
        merged.update(parse_dotenv_file(ref_path))

    file_count = len(file_refs)
    file_key_count = len(merged)

    for entry in config.get('envs', []):
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            merged[key] = str(value)

    config['envs'] = [{k: v} for k, v in merged.items()]
    logger.info(
        f"Loaded {file_key_count} env var(s) from {file_count} file(s) referenced by "
        f"envs_from_files in {label}"
    )

def load_and_validate_config(yaml_file_path: str, service_name: Optional[str] = None) -> Dict[str, Any]:
    """Load and validate YAML configuration, applying service overrides if present"""
    try:
        yaml_path = Path(yaml_file_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")

        with yaml_path.open('r') as file:
            raw_config = yaml.safe_load(file)

        if not raw_config:
            raise ValidationError("YAML file is empty or invalid")

        # Validate services_overrides structure before applying
        validate_services_overrides(raw_config)

        # Apply service-specific overrides if present
        config = apply_service_overrides(raw_config, service_name)

        # Shape-check sidecars before anything iterates them, so `sidecars: oops`
        # fails with a real message instead of an AttributeError below.
        validate_sidecars(config)

        # Expand envs_from_files into the envs list (after merge so per-service
        # entries are appended; before validation so the final envs list is what
        # validate_config sees). Sidecars resolve their files against the same
        # YAML directory, but only into their own envs.
        expand_envs_from_files(config, yaml_path)
        # Only the sidecars that will actually be rendered: a switched-off base
        # sidecar must not fail every deploy because its dotenv file was deleted
        # along with it.
        for sidecar in enabled_sidecars(config):
            expand_envs_from_files(sidecar, yaml_path, label=f"sidecar '{sidecar.get('name')}'")

        # Validate the final merged config
        validate_config(config)
        logger.info(f"Successfully loaded and validated configuration from {yaml_file_path}")
        if service_name and raw_config.get('services_overrides', {}).get(service_name):
            logger.info(f"Applied overrides for service: {service_name}")
        return config

    except yaml.YAMLError as e:
        raise ValidationError(f"Invalid YAML format: {e}")

class ContainerBuilder:
    """Builder class for container configurations"""
    
    def __init__(self, cluster_name: str, app_name: str, aws_region: str):
        self.cluster_name = cluster_name
        self.app_name = app_name
        self.aws_region = aws_region
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
    
    def build_log_configuration(self, log_driver: str = "awslogs", 
                              stream_prefix: str = "default") -> Dict[str, Any]:
        """Build standard log configuration"""
        # Add leading slash only for "default" stream prefix for compatibility
        if stream_prefix == "default":
            stream_prefix = "/default"
            
        return {
            "logDriver": log_driver,
            "options": {
                "awslogs-group": f"/ecs/{self.cluster_name}/{self.app_name}",
                "awslogs-region": self.aws_region,
                "awslogs-stream-prefix": stream_prefix
            }
        }
    
    def build_port_mappings(self, main_port: Optional[int],
                           additional_ports: List[Dict[str, int]], app_protocol: str = "http",
                           network_mode: str = "awsvpc",
                           main_port_name: str = "default") -> List[Dict[str, Any]]:
        """Build port mappings configuration

        Args:
            main_port: Primary container port
            additional_ports: List of additional port mappings
            app_protocol: Application protocol (http, grpc, tcp)
            network_mode: Network mode (awsvpc, bridge, host, none)
            main_port_name: Name of the primary port mapping. Port mapping names
                must be unique across the whole task definition, so a sidecar
                cannot reuse the application's "default".
        """
        port_mappings = []
        
        # For bridge mode, hostPort can be 0 (dynamic) or different from containerPort
        # For awsvpc/host modes, hostPort must equal containerPort
        use_dynamic_host_port = network_mode == 'bridge'
        
        if main_port:
            port_mapping = {
                "name": main_port_name,
                "containerPort": main_port,
                "hostPort": 0 if use_dynamic_host_port else main_port,
                "protocol": "tcp"
            }
            if app_protocol != "tcp":
                port_mapping["appProtocol"] = app_protocol
            port_mappings.append(port_mapping)
        
        for port_info in additional_ports:
            if isinstance(port_info, dict):
                for name, port in port_info.items():
                    port_mapping = {
                        "name": name,
                        "containerPort": port,
                        "hostPort": 0 if use_dynamic_host_port else port,
                        "protocol": "tcp"
                    }
                    if app_protocol != "tcp":
                        port_mapping["appProtocol"] = app_protocol
                    port_mappings.append(port_mapping)
        
        self.logger.debug(f"Built {len(port_mappings)} port mappings (network_mode={network_mode})")
        return port_mappings

class RoleResolver:
    """Resolve taskRoleArn / executionRoleArn for a task definition.

    Precedence, evaluated independently per slot:

      1. per-slot YAML key  task_role_arn / execution_role_arn
      2. shared YAML key    role_arn
      3. SSM parameter published by terraform-aws-ecs-service (>= v2.0.0):
           /ecs/<cluster>/<service>/task-role
           /ecs/<cluster>/<service>/execution-role

    Before v3.0.0 the module derived both slots from a single shared role, so
    reusing one ARN for both happened to work. v3 made them independent
    identities, so each slot is resolved on its own.

    Both slots are mandatory: every task has a task role and an execution role.

    Unlike SecretManager below, this class NEVER substitutes mock values. An
    unresolved slot, or any AWS fault, is a hard failure - a task definition
    silently registered with the wrong role is worse than a failed deploy.
    """

    # (yaml key, task-definition key, SSM parameter suffix)
    SLOTS = (
        ("task_role_arn", "taskRoleArn", "task-role"),
        ("execution_role_arn", "executionRoleArn", "execution-role"),
    )

    def __init__(self, cluster_name: str, service_name: str, aws_region: str):
        self.cluster_name = cluster_name
        self.service_name = service_name
        self.aws_region = aws_region
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def resolve(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Return the task-definition role keys mapped to their ARNs.

        The caller can splat the result straight into the task definition dict.
        """
        shared = normalize_role_value(config, 'role_arn', RoleResolutionError)

        resolved: Dict[str, str] = {}
        pending: Dict[str, tuple] = {}  # ssm parameter name -> (td_key, yaml_key)

        for yaml_key, td_key, suffix in self.SLOTS:
            # Track which key actually supplied the value, so the log line and
            # any error name a key the user can really find in their YAML.
            value = normalize_role_value(config, yaml_key, RoleResolutionError)
            source_key = yaml_key
            if value is None:
                value, source_key = shared, 'role_arn'

            if value is None:
                pending[self._ssm_name(suffix)] = (td_key, yaml_key)
                continue

            validate_role_arn(value, f"YAML key '{source_key}'", RoleResolutionError)
            resolved[td_key] = value
            self.logger.info(f"{td_key} taken from YAML key '{source_key}'")

        if pending:
            resolved.update(self._resolve_from_ssm(pending))

        # Emit in SLOTS order so JSON key order is stable regardless of source.
        return {td_key: resolved[td_key] for _, td_key, _ in self.SLOTS if td_key in resolved}

    def _ssm_name(self, suffix: str) -> str:
        return f"/ecs/{self.cluster_name}/{self.service_name}/{suffix}"

    def _resolve_from_ssm(self, pending: Dict[str, tuple]) -> Dict[str, str]:
        """Batch-read the outstanding role ARNs from SSM Parameter Store."""
        # Imported lazily so a fully YAML-configured deploy needs neither boto3
        # nor AWS credentials. The test suite relies on this.
        try:
            import boto3
            from botocore.exceptions import (
                BotoCoreError, ClientError, NoCredentialsError,
                PartialCredentialsError, TokenRetrievalError, NoRegionError,
                EndpointConnectionError,
            )
        except ImportError as e:
            raise RoleResolutionError(
                f"boto3 is required to read role ARNs from SSM but could not be "
                f"imported ({e}). Install requirements.txt, or set task_role_arn / "
                f"execution_role_arn (or role_arn) in the task config YAML."
            ) from e

        names = sorted(pending)
        joined = ', '.join(names)
        self.logger.info(f"Resolving role ARNs from SSM: {joined}")

        try:
            client = boto3.Session().client('ssm', region_name=self.aws_region)
            response = client.get_parameters(Names=names, WithDecryption=True)
        except (NoCredentialsError, PartialCredentialsError, TokenRetrievalError) as e:
            raise RoleResolutionError(
                f"Cannot read role ARNs from SSM: no usable AWS credentials "
                f"({type(e).__name__}). Parameters needed: {joined}. Configure AWS "
                f"credentials for this step, or set task_role_arn / "
                f"execution_role_arn (or role_arn) in the task config YAML."
            ) from e
        except NoRegionError as e:
            raise RoleResolutionError(
                f"Cannot read role ARNs from SSM: no AWS region configured "
                f"(aws_region={self.aws_region!r})."
            ) from e
        except EndpointConnectionError as e:
            raise RoleResolutionError(
                f"Cannot reach the SSM endpoint in region '{self.aws_region}': {e}"
            ) from e
        except ClientError as e:
            raise self._client_error(e, names) from e
        except BotoCoreError as e:
            # Catch-all for the rest of botocore's tree: connect/read timeouts,
            # SSL and proxy failures, credential-provider errors, bad profiles.
            # ClientError is not a BotoCoreError subclass, so this cannot shadow
            # the taxonomy above.
            raise RoleResolutionError(
                f"Failed to read role ARNs from SSM ({joined}): "
                f"{type(e).__name__}: {e}. Alternatively set task_role_arn / "
                f"execution_role_arn (or role_arn) in the task config YAML."
            ) from e

        # Names that do not exist are returned in InvalidParameters rather than
        # raising - ParameterNotFound is an error shape of GetParameter
        # (singular) only. A name the caller is not authorized for also tends to
        # land here, which is why the failure message names both causes.
        invalid = response.get('InvalidParameters', [])
        if invalid:
            self.logger.debug(f"SSM returned InvalidParameters: {invalid}")

        values = {
            p['Name']: (p.get('Value') or '').strip()
            for p in response.get('Parameters', [])
        }

        resolved: Dict[str, str] = {}
        missing = []
        for name, (td_key, yaml_key) in pending.items():
            value = values.get(name)
            if not value:
                missing.append((name, td_key, yaml_key))
                continue
            validate_role_arn(value, f"SSM parameter {name}", RoleResolutionError)
            resolved[td_key] = value
            self.logger.info(f"{td_key} resolved from SSM parameter {name}")

        if missing:
            raise RoleResolutionError(self._unresolved_message(missing))

        return resolved

    def _client_error(self, error, names: List[str]) -> RoleResolutionError:
        """Translate a botocore ClientError into an actionable message."""
        code = error.response.get('Error', {}).get('Code', 'Unknown')
        joined = ', '.join(names)

        if code in ('AccessDeniedException', 'AccessDenied', 'UnauthorizedOperation'):
            return RoleResolutionError(
                f"Access denied reading role ARNs from SSM ({joined}). The deploy "
                f"role needs ssm:GetParameters (plural) on "
                f"arn:aws:ssm:{self.aws_region}:<account-id>:parameter/ecs/"
                f"{self.cluster_name}/{self.service_name}/*. Alternatively set "
                f"task_role_arn / execution_role_arn (or role_arn) in the YAML."
            )
        if code in ('ThrottlingException', 'TooManyUpdates', 'RequestLimitExceeded'):
            return RoleResolutionError(
                f"SSM throttled the role lookup ({code}) for {joined}. Retry the deploy."
            )
        if code in ('ExpiredTokenException', 'ExpiredToken',
                    'UnrecognizedClientException', 'InvalidClientTokenId'):
            return RoleResolutionError(
                f"AWS credentials are expired or invalid ({code}) while reading role "
                f"ARNs from SSM ({joined})."
            )
        return RoleResolutionError(
            f"AWS error {code} reading role ARNs from SSM ({joined}): {error}"
        )

    def _unresolved_message(self, missing: List[tuple]) -> str:
        """One block per unresolved slot, so both are reported in one run."""
        blocks = []
        for name, td_key, yaml_key in missing:
            width = max(len(yaml_key), len('role_arn')) + 2
            per_slot = f"'{yaml_key}'".ljust(width)
            shared = "'role_arn'".ljust(width)
            blocks.append(
                f"Could not determine {td_key} for service '{self.service_name}' in "
                f"cluster '{self.cluster_name}'.\n"
                f"  Tried, in order:\n"
                f"    1. YAML key {per_slot} - not set\n"
                f"    2. YAML key {shared} - not set\n"
                f"    3. SSM parameter {name}\n"
                f"       - not returned (it does not exist, or the deploy role lacks\n"
                f"         ssm:GetParameters on it)\n"
                f"  Fix one of:\n"
                f"    * set '{yaml_key}' (or 'role_arn') in the task config YAML; or\n"
                f"    * let terraform-aws-ecs-service create the role - it publishes\n"
                f"      {name} automatically.\n"
                f"  Note: scheduled_task and triggerable_task deployments are not managed\n"
                f"  by the ECS service module and have no such SSM parameter - set the\n"
                f"  role ARNs in YAML for those."
            )
        return "\n\n".join(blocks)

class SecretManager:
    """Handle secrets configuration"""
    
    @staticmethod
    def discover_secret_keys(secret_name: str) -> tuple[List[str], str]:
        """Discover all keys in a secret by querying AWS Secrets Manager
        
        Returns:
            tuple: (list_of_keys, full_secret_arn)
        """
        import boto3
        import json
        from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError, TokenRetrievalError
        
        try:
            # Create a Secrets Manager client
            session = boto3.Session()
            client = session.client('secretsmanager')
            
            # Get the secret value
            response = client.get_secret_value(SecretId=secret_name)
            secret_string = response['SecretString']
            full_secret_arn = response['ARN']  # Get the full ARN with suffix
            
            # Parse the JSON to get the keys
            secret_data = json.loads(secret_string)
            
            if isinstance(secret_data, dict):
                keys = list(secret_data.keys())
                return keys, full_secret_arn
            else:
                logger.warning(f"Secret '{secret_name}' does not contain a JSON object")
                return [], full_secret_arn
                
        except (NoCredentialsError, PartialCredentialsError, TokenRetrievalError):
            # For testing environments where AWS credentials aren't available or expired
            logger.warning(f"AWS credentials not available or expired. Using mock keys for secret '{secret_name}'")
            keys = SecretManager._get_mock_keys(secret_name)
            mock_arn = SecretManager._get_mock_arn(secret_name)
            return keys, mock_arn
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'ResourceNotFoundException':
                logger.error(f"Secret '{secret_name}' not found")
                # Fall back to mock keys for testing
                logger.warning(f"Falling back to mock keys for secret '{secret_name}'")
                keys = SecretManager._get_mock_keys(secret_name)
                mock_arn = SecretManager._get_mock_arn(secret_name)
                return keys, mock_arn
            else:
                logger.error(f"AWS error discovering keys for secret '{secret_name}': {e}")
                # Fall back to mock keys for testing
                logger.warning(f"Falling back to mock keys for secret '{secret_name}'")
                keys = SecretManager._get_mock_keys(secret_name)
                mock_arn = SecretManager._get_mock_arn(secret_name)
                return keys, mock_arn
        except Exception as e:
            logger.error(f"Error discovering keys for secret '{secret_name}': {e}")
            # Fall back to mock keys for testing
            logger.warning(f"Falling back to mock keys for secret '{secret_name}'")
            keys = SecretManager._get_mock_keys(secret_name)
            mock_arn = SecretManager._get_mock_arn(secret_name)
            return keys, mock_arn
    
    @staticmethod
    def _get_mock_keys(secret_name: str) -> List[str]:
        """Return mock keys for testing when AWS credentials aren't available"""
        # Mock data based on common secret patterns
        mock_keys = {
            'database-credentials': ['DB_HOST', 'DB_PORT', 'DB_USERNAME', 'DB_PASSWORD'],
            'oauth-config': ['CLIENT_ID', 'CLIENT_SECRET', 'REDIRECT_URL'],
            'api-keys': ['EXTERNAL_API_KEY', 'WEBHOOK_SECRET'],
            'certificates': ['SSL_CERT', 'SSL_KEY']
        }
        
        # Try to find a match by partial name
        for pattern, keys in mock_keys.items():
            if pattern in secret_name.lower():
                return keys
        
        # Default fallback
        return ['SECRET_KEY', 'SECRET_VALUE']
    
    @staticmethod
    def _get_mock_arn(secret_name: str) -> str:
        """Return mock ARN for testing when AWS credentials aren't available"""
        # Mock ARN patterns based on common secret names
        mock_suffixes = {
            'database-credentials': 'abc123',
            'oauth-config': 'def456', 
            'api-keys': 'ghi789',
            'certificates': 'jkl012'
        }
        
        # Try to find a match by partial name
        for pattern, suffix in mock_suffixes.items():
            if pattern in secret_name.lower():
                return f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{secret_name}-{suffix}"
        
        # Default fallback
        return f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{secret_name}-xyz789"
    
    @staticmethod
    def build_secrets_from_config(config: Dict[str, Any]) -> List[Dict[str, str]]:
        """Build secrets configuration from YAML config"""
        secrets = []
        
        # Legacy format support
        secret_list = config.get('secrets') or []
        if secret_list:
            for secret_dict in secret_list:
                for key, base_arn in secret_dict.items():
                    secrets.append({
                        "name": key,
                        "valueFrom": f"{base_arn}:{key}::"
                    })
            logger.info(f"Built {len(secrets)} secret configurations (legacy format)")
            return secrets
        
        # New format
        secrets_envs = config.get('secrets_envs') or []
        
        for secret_config in secrets_envs:
            secret_id = secret_config.get('id', '')
            secret_name = secret_config.get('name', '')
            secret_values = secret_config.get('values', [])
            
            # Handle name-only format (new feature) - query AWS to get keys
            if secret_name and not secret_id and not secret_values:
                try:
                    # Query AWS Secrets Manager to discover keys in this secret
                    discovered_keys, full_secret_arn = SecretManager.discover_secret_keys(secret_name)
                    if discovered_keys:
                        for key in discovered_keys:
                            secrets.append({
                                "name": key,
                                "valueFrom": f"{full_secret_arn}:{key}::"
                            })
                        logger.info(f"Auto-discovered {len(discovered_keys)} keys from secret '{secret_name}': {discovered_keys}")
                        logger.info(f"Using full secret ARN: {full_secret_arn}")
                    else:
                        logger.warning(f"No keys found in secret '{secret_name}'")
                except Exception as e:
                    logger.error(f"Failed to discover keys for secret '{secret_name}': {e}")
                continue
            
            # Handle traditional id + values format
            if not secret_id:
                logger.warning("Secret configuration missing 'id' field")
                continue
                
            for key in secret_values:
                secrets.append({
                    "name": key,
                    "valueFrom": f"{secret_id}:{key}::"
                })
        
        logger.info(f"Built {len(secrets)} secret configurations (new format)")
        return secrets

def parse_image_parts(image_name: str, tag: str) -> tuple[str, str]:
    """Parse and clean image name and tag"""
    logger.debug(f"Parsing image parts: image_name='{image_name}', tag='{tag}'")
    
    # Remove registry if mistakenly included in image_name
    if '/' in image_name and '.' in image_name.split('/')[0]:
        # Remove registry part
        image_name = '/'.join(image_name.split('/')[1:])
        logger.debug(f"Removed registry from image_name: '{image_name}'")
    
    # Remove tag from image_name if present
    if ':' in image_name:
        image_name, image_tag = image_name.split(':', 1)
        if not tag:
            tag = image_tag
            logger.debug(f"Extracted tag from image_name: '{tag}'")
    
    return image_name, tag

def build_image_uri(container_registry: Optional[str], image_name: str, tag: str) -> str:
    """Build container image URI with proper validation"""
    logger.debug(f"Building image URI: registry={container_registry}, image={image_name}, tag={tag}")
    
    # Clean image name and tag
    image_name_clean, tag_clean = parse_image_parts(image_name, tag)
    
    if container_registry and container_registry.strip():
        image_uri = f"{container_registry}/{image_name_clean}:{tag_clean}"
    else:
        image_uri = f"{image_name_clean}:{tag_clean}"
    
    logger.info(f"Container image URI: {image_uri}")
    return image_uri

def build_environment(container_config: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build the `environment` list from a container's `envs` block.

    Shared by the application container and every generic sidecar. Values are
    stringified because ECS only accepts strings, so `ENABLE_METRICS: true`
    becomes "True" - long-standing behaviour that existing goldens assert.
    """
    environment = []
    for env_var in container_config.get('envs') or []:
        for key, value in env_var.items():
            environment.append({
                "name": key,
                "value": str(value)
            })
    return environment

def build_health_check(container_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a container `healthCheck` from a `health_check` block, or None.

    Shared by the application container and every generic sidecar. A block with
    no (or an empty) command yields no healthCheck at all rather than a broken
    one.
    """
    health_check = container_config.get('health_check', {})
    if not health_check or not health_check.get('command'):
        return None

    return {
        "command": ["CMD-SHELL", health_check["command"]],
        "interval": health_check.get('interval', 30),
        "timeout": health_check.get('timeout', 5),
        "retries": health_check.get('retries', 3),
        "startPeriod": health_check.get('start_period', 10)
    }

def build_init_containers(config, secret_files, cluster_name, app_name, aws_region,
                          secrets_files_path="/etc/secrets", *,
                          container_name="init-container-for-secret-files",
                          source_volume="shared-volume",
                          log_stream_prefix="ssm-file-downloader"):
    """Build init containers for secret file downloads

    The keyword-only arguments default to the application's long-standing names.
    Sidecars pass their own prefixed names so two containers that both want
    secret files do not fight over one container name and one volume.
    """
    container_definitions = []
    
    # Handle secret files (existing functionality)
    if secret_files:
        # Join secret names with commas for the environment variable
        secret_files_env = ",".join(secret_files)
        
        container_builder = ContainerBuilder(cluster_name, app_name, aws_region)
        
        init_container = {
            "name": container_name,
            "image": "public.ecr.aws/aws-cli/aws-cli:latest",
            "essential": False,
            "entryPoint": ["/bin/sh"],
            "command": [
                "-c",
                f"for secret in ${{SECRET_FILES//,/ }}; do "
                f"  echo \"Fetching $secret...\"; "
                f"  echo \"Debug: AWS_REGION=$AWS_REGION, SECRET_PATH={secrets_files_path}\"; "
                f"  SECRET_VALUE=$(aws secretsmanager get-secret-value --secret-id $secret --region $AWS_REGION --query SecretString --output text 2>/dev/null); "
                f"  STRING_RESULT=$?; "
                f"  if [ $STRING_RESULT -eq 0 ] && [ -n \"$SECRET_VALUE\" ] && [ \"$SECRET_VALUE\" != \"null\" ] && [ \"$SECRET_VALUE\" != \"none\" ] && [ \"$SECRET_VALUE\" != \"None\" ]; then "
                f"    echo \"Found text secret, saving to {secrets_files_path}/$secret\"; "
                f"    echo \"$SECRET_VALUE\" > {secrets_files_path}/$secret; "
                f"  else "
                f"    echo \"Text retrieval failed or returned null, trying binary retrieval...\"; "
                f"    aws secretsmanager get-secret-value --secret-id $secret --region $AWS_REGION --query SecretBinary --output text | base64 -d > {secrets_files_path}/$secret 2>/dev/null; "
                f"    BINARY_RESULT=$?; "
                f"    if [ $BINARY_RESULT -eq 0 ] && [ -s {secrets_files_path}/$secret ]; then "
                f"      echo \"Found binary secret, saved to {secrets_files_path}/$secret\"; "
                f"    else "
                f"      echo \"❌ Failed to retrieve $secret as either text or binary\" >&2; "
                f"      echo \"Text result: $STRING_RESULT, Binary result: $BINARY_RESULT\" >&2; "
                f"      exit 1; "
                f"    fi; "
                f"  fi; "
                f"  echo \"✅ Successfully saved $secret to {secrets_files_path}/$secret (size: $(stat -c%s {secrets_files_path}/$secret 2>/dev/null || wc -c < {secrets_files_path}/$secret))\"; "
                f"done"
            ],
            "environment": [
                {
                    "name": "SECRET_FILES",
                    "value": secret_files_env
                },
                {
                    "name": "AWS_REGION",
                    "value": aws_region
                }
            ],
            "mountPoints": [
                {
                    "sourceVolume": source_volume,
                    "containerPath": secrets_files_path
                }
            ],
            "logConfiguration": container_builder.build_log_configuration(stream_prefix=log_stream_prefix)
        }
        container_definitions.append(init_container)
        logger.info(f"Built init container '{container_name}' for {len(secret_files)} secret files")
    
    return container_definitions

def build_linux_parameters(config: Dict[str, Any], launch_type: str = "FARGATE") -> Optional[Dict[str, Any]]:
    """Build linuxParameters for container definition
    
    Args:
        config: The YAML configuration dictionary
        launch_type: Launch type (FARGATE or EC2)
    
    Returns:
        Dict with linuxParameters or None if not configured
    """
    linux_params = config.get('linux_parameters', {})
    if not linux_params:
        return None
    
    linux_parameters = {}
    
    # Parameters supported by both Fargate and EC2
    init_process_enabled = linux_params.get('init_process_enabled')
    if init_process_enabled is not None:
        linux_parameters["initProcessEnabled"] = bool(init_process_enabled)
        logger.info(f"Set initProcessEnabled to {bool(init_process_enabled)}")
    
    # Capabilities (add/drop) - supported by both Fargate and EC2
    capabilities = linux_params.get('capabilities', {})
    if capabilities:
        caps = {}
        if 'add' in capabilities and capabilities['add']:
            caps["add"] = list(capabilities['add'])
        if 'drop' in capabilities and capabilities['drop']:
            caps["drop"] = list(capabilities['drop'])
        if caps:
            linux_parameters["capabilities"] = caps
            logger.info(f"Set capabilities: add={caps.get('add', [])}, drop={caps.get('drop', [])}")
    
    # tmpfs mounts - supported by both Fargate and EC2
    tmpfs_config = linux_params.get('tmpfs', [])
    if tmpfs_config:
        tmpfs_mounts = []
        for mount in tmpfs_config:
            container_path = mount.get('container_path') or '/tmp'
            raw_size = mount.get('size', 64)
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                raise ValidationError(f"Invalid tmpfs size '{raw_size}' for mount {mount!r}. Size must be a positive integer.")
            if size <= 0:
                raise ValidationError(f"Invalid tmpfs size '{raw_size}' for mount {mount!r}. Size must be a positive integer greater than zero.")
            tmpfs_mount = {
                "containerPath": mount.get('container_path', '/tmp'),
                "size": size
            }
            mount_options = mount.get('mount_options', [])
            if mount_options:
                tmpfs_mount["mountOptions"] = list(mount_options)
            tmpfs_mounts.append(tmpfs_mount)
        if tmpfs_mounts:
            linux_parameters["tmpfs"] = tmpfs_mounts
            logger.info(f"Set {len(tmpfs_mounts)} tmpfs mounts")
    
    # swappiness - supported by Fargate (1.4.0+) and EC2
    swappiness = linux_params.get('swappiness')
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
    max_swap = linux_params.get('max_swap')
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
    shared_memory_size = linux_params.get('shared_memory_size')
    if shared_memory_size is not None:
        if launch_type == 'FARGATE':
            logger.warning(f"shared_memory_size is EC2-only, ignoring for Fargate launch type")
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
    devices_config = linux_params.get('devices', [])
    if devices_config:
        if launch_type == 'FARGATE':
            logger.warning(f"devices is EC2-only, ignoring for Fargate launch type")
        else:
            devices = []
            for device in devices_config:
                host_path = device.get('host_path')
                if not host_path:
                    raise ValidationError(
                        "Each entry in linux_parameters.devices must include a non-empty 'host_path'. "
                        f"Invalid device mapping: {device}"
                    )
                container_path = device.get('container_path', host_path)
                permissions = device.get('permissions', ['read', 'write'])
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

def build_container_base(spec, container_builder, *, name, image, essential,
                         log_configuration, environment, secrets, health,
                         network_mode="awsvpc", launch_type="FARGATE",
                         main_port_name="default"):
    """Build the parts of a container definition that every container shares.

    `spec` is the config block that owns the container: the whole task config
    for the application container, or a single `sidecars` entry. Everything read
    from it - command, entrypoint, stop_timeout, ports, health check, linux
    parameters - therefore comes from that container's own configuration, which
    is what keeps sidecars isolated from the application.

    Callers add whatever is specific to them (mount points, dependencies,
    resource reservations, readonlyRootFilesystem) to the returned dict.
    """
    # `or []` rather than a .get() default throughout: a key written with no
    # value (`command:`) parses as None, and validation treats that as "unset".
    # Emitting `"command": null` would be rejected by RegisterTaskDefinition
    # after every offline check had already passed.
    container = {
        "name": name,
        "image": image,
        "essential": essential,
        "environment": environment,
        "command": spec.get('command') or [],
        "entryPoint": spec.get('entrypoint') or [],
        "secrets": secrets,
    }

    # Add stopTimeout if specified
    stop_timeout = spec.get('stop_timeout')
    if stop_timeout is not None:
        container["stopTimeout"] = int(stop_timeout)

    container["logConfiguration"] = log_configuration

    # Only include healthCheck if it was properly built
    if health:
        container["healthCheck"] = health

    # Handle port configurations
    port_mappings = container_builder.build_port_mappings(
        spec.get('port'),
        spec.get('additional_ports') or [],
        spec.get('app_protocol') or 'http',
        network_mode,
        main_port_name,
    )
    if port_mappings:
        container["portMappings"] = port_mappings

    # Add linuxParameters if configured
    linux_parameters = build_linux_parameters(spec, launch_type)
    if linux_parameters:
        container["linuxParameters"] = linux_parameters

    return container

def build_app_container(config, image_uri, environment, secrets, health, cluster_name, app_name, aws_region, use_fluent_bit, has_secret_files, secrets_files_path="/etc/secrets", network_mode="awsvpc", launch_type="FARGATE"):
    """
    Build the main application container definition for the ECS task.

    Args:
        config: Application configuration dictionary used to derive container
            properties such as command, entryPoint, ports, and linux parameters.
        image_uri: Full URI of the container image to run.
        environment: List of environment variable definitions to inject into
            the container.
        secrets: List of secret definitions to inject into the container.
        health: Optional health check configuration dictionary. If provided,
            it is added as the container's ``healthCheck``.
        cluster_name: Name of the ECS cluster used for log configuration and
            other contextual metadata.
        app_name: Logical name of the application, used for log configuration
            and identifying the container.
        aws_region: AWS region where the task definition will be used.
        use_fluent_bit: If True, configures the container to use a FireLens
            (fluent-bit) sidecar for logging; otherwise uses awslogs directly.
        has_secret_files: If True, mounts a shared volume at
            ``secrets_files_path`` and adds a dependency on the init container
            that populates secret files.
        secrets_files_path: Container path where secret files should be
            mounted when ``has_secret_files`` is True. Defaults to
            ``"/etc/secrets"``.
        network_mode: The network mode of the task (for example ``"awsvpc"``
            or ``"bridge"``). Used when building port mappings to ensure they
            are compatible with the task's networking configuration.
        launch_type: The ECS launch type for the task (for example
            ``"FARGATE"`` or ``"EC2"``). Passed through to
            :func:`build_linux_parameters` to determine which Linux-specific
            parameters are valid.

    Returns:
        A dictionary describing the main application container suitable for
        inclusion in an ECS task definition.
    """
    container_builder = ContainerBuilder(cluster_name, app_name, aws_region)

    # Set logConfiguration for app container
    if use_fluent_bit:
        log_configuration = {
            "logDriver": "awsfirelens",
            "options": {}
        }
    else:
        log_configuration = container_builder.build_log_configuration(stream_prefix="default")

    app_container = build_container_base(
        config, container_builder,
        name="app",
        image=image_uri,
        essential=True,
        log_configuration=log_configuration,
        environment=environment,
        secrets=secrets,
        health=health,
        network_mode=network_mode,
        launch_type=launch_type,
    )

    # Add mount points if using shared volume
    if has_secret_files:
        app_container["mountPoints"] = [
            {
                "sourceVolume": "shared-volume",
                "containerPath": secrets_files_path
            }
        ]
        # Add dependency on init containers
        app_depends_on = [
            {
                "containerName": "init-container-for-secret-files",
                "condition": "SUCCESS"
            }
        ]
    else:
        app_depends_on = []

    # If fluent-bit is enabled, add dependsOn for fluent-bit
    if use_fluent_bit:
        app_depends_on.append({
            "containerName": "fluent-bit",
            "condition": "START"
        })
    if app_depends_on:
        app_container["dependsOn"] = app_depends_on
    
    return app_container

def build_fluent_bit_container(config, fluent_bit_image, app_name, cluster_name, aws_region):
    """Build Fluent Bit sidecar container"""
    fluent_bit_collector = config.get('fluent_bit_collector', {})
    config_name = fluent_bit_collector.get('extra_config', "extra.conf")
    ecs_log_metadata = fluent_bit_collector.get('ecs_log_metadata', 'true')
    # Allow custom service_name, default to app_name if not specified
    fluent_bit_service_name = fluent_bit_collector.get('service_name', app_name)
    extra_config = f"extra/{config_name}"
    
    fluent_bit_container = {
        "name": "fluent-bit",
        "image": fluent_bit_image,  # Always ECR-style
        "essential": True,  # Critical sidecar - if it fails, task should fail
        "environment": [
            {"name": "SERVICE_NAME", "value": fluent_bit_service_name},
            {"name": "ENV", "value": cluster_name}
        ],
        "healthCheck": {
            "command": [
                "CMD-SHELL",
                "curl -f http://127.0.0.1:2020/api/v1/health || exit 1"
            ],
            "interval": 10,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 5
        },
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "fluentbit"
            }
        },
        "firelensConfiguration": {
            "type": "fluentbit",
            "options": {
                "config-file-type": "file",
                "config-file-value": extra_config,
                "enable-ecs-log-metadata": ecs_log_metadata
            }
        }
    }
    
    return fluent_bit_container

def build_otel_container(config, otel_collector_image, otel_is_custom_image, otel_collector_ssm, otel_extra_config, otel_metrics_port, otel_metrics_path, app_name, cluster_name, aws_region):
    """Build OpenTelemetry collector container"""
    # Build environment variables for OTEL container
    otel_environment = []
    
    # Always add METRICS_PATH (default: /metrics)
    otel_environment.append({
        "name": "METRICS_PATH",
        "value": otel_metrics_path
    })
    
    # Always add METRICS_PORT (default: 8080)
    otel_environment.append({
        "name": "METRICS_PORT",
        "value": str(otel_metrics_port)
    })
    
    # Add SERVICE_NAME if using custom image (not default AWS image)
    if otel_is_custom_image:
        otel_environment.append({
            "name": "SERVICE_NAME",
            "value": app_name
        })
    
    # Build command based on image type
    if otel_is_custom_image and otel_extra_config:
        # Custom image with extra config file
        otel_command = [
            "--config",
            f"/conf/{otel_extra_config}"
        ]
    elif otel_is_custom_image:
        # Custom image without extra config (use default config path)
        otel_command = [
            "--config",
            "/conf/config.yaml"
        ]
    else:
        # Default AWS image - use SSM config
        otel_command = [
            "--config",
            "env:SSM_CONFIG"
        ]
    
    otel_container = {
        "name": "otel-collector",
        "image": otel_collector_image,  # Use as-is from YAML or default
        "portMappings": [
            {
                "name": "otel-collector-4317-tcp",
                "containerPort": 4317,
                "hostPort": 4317,
                "protocol": "tcp",
                "appProtocol": "grpc"
            },
            {
                "name": "otel-collector-4318-tcp",
                "containerPort": 4318,
                "hostPort": 4318,
                "protocol": "tcp"
            }
        ],
        "essential": True,  # Critical sidecar - if it fails, task should fail
        "command": otel_command,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{cluster_name}/{app_name}",
                "awslogs-region": aws_region,
                "awslogs-stream-prefix": "otel-collector"
            }
        }
    }
    
    # Add environment variables if any
    if otel_environment:
        otel_container["environment"] = otel_environment
    
    # Add secrets only for default AWS image
    if not otel_is_custom_image:
        otel_container["secrets"] = [
            {
                "name": "SSM_CONFIG",
                "valueFrom": otel_collector_ssm
            }
        ]
    
    return otel_container

def build_sidecar_container(sidecar, config, cluster_name, app_name, aws_region,
                            network_mode="awsvpc", launch_type="FARGATE"):
    """Build one generic sidecar, plus the init container and volumes it needs.

    Isolation is the whole point: nothing from the application container leaks
    in. A sidecar's environment, secrets, secret files, mounts, ports, health
    check and linux parameters come exclusively from its own block. The single
    inherited value is readonly_root_filesystem, and only as a fallback when the
    sidecar does not state one of its own.

    Args:
        sidecar: One entry of the `sidecars` list, already validated.
        config: The full task config, read only for the readonly_root_filesystem
            fallback.

    Returns:
        (container_definitions, volumes). When the sidecar declares secret_files
        its init container precedes it, so `dependsOn` is satisfiable.
    """
    name = sidecar['name']

    if sidecar.get('envs_from_files'):
        # Normally expanded in load_and_validate_config. Reaching here means the
        # caller built a config dict by hand and skipped that step; silently
        # dropping the file would ship a sidecar missing its environment.
        raise ValidationError(
            f"sidecar '{name}': envs_from_files was not expanded. It is only "
            f"supported when the config is loaded from a YAML file."
        )

    builder = ContainerBuilder(cluster_name, app_name, aws_region)
    containers = []
    volumes = []

    main_port = sidecar.get('port')
    container = build_container_base(
        sidecar, builder,
        name=name,
        image=sidecar['image'],
        # Only an explicit `essential: false` makes a sidecar non-essential. A
        # blank `essential:` parses as None, and bool(None) would silently mean
        # "let this container die without failing the task".
        essential=sidecar.get('essential') is not False,
        # Sidecars always log straight to CloudWatch under the service log
        # group, exactly like the fluent-bit and otel-collector containers -
        # they do not follow the application onto awsfirelens.
        log_configuration=builder.build_log_configuration(
            stream_prefix=sidecar.get('log_stream_prefix', name)
        ),
        environment=build_environment(sidecar),
        secrets=SecretManager.build_secrets_from_config(sidecar),
        health=build_health_check(sidecar),
        network_mode=network_mode,
        launch_type=launch_type,
        main_port_name=_sidecar_main_port_name(name, main_port) if main_port else name,
    )

    # Container-level reservations. Task-level cpu/memory are strings; the
    # container-level equivalents must be integers.
    for yaml_key, td_key in (('cpu', 'cpu'), ('memory', 'memory'),
                             ('memory_reservation', 'memoryReservation')):
        value = sidecar.get(yaml_key)
        if value is not None:
            container[td_key] = int(value)

    # Per-container precedence: the sidecar's own value wins, otherwise the
    # application-level default, otherwise the key is omitted entirely.
    readonly = sidecar.get('readonly_root_filesystem')
    if readonly is None:
        readonly = config.get('readonly_root_filesystem')
    if readonly is not None:
        container["readonlyRootFilesystem"] = bool(readonly)

    mount_points = []
    depends_on = []

    secret_files = sidecar.get('secret_files') or []
    if secret_files:
        secrets_files_path = sidecar.get('secrets_files_path', '/etc/secrets')
        init_name = sidecar_init_container_name(name)
        volume_name = sidecar_secrets_volume_name(name)

        init_containers = build_init_containers(
            sidecar, secret_files, cluster_name, app_name, aws_region,
            secrets_files_path,
            container_name=init_name,
            source_volume=volume_name,
            log_stream_prefix=init_name,
        )
        for init_container in init_containers:
            if readonly is not None:
                init_container["readonlyRootFilesystem"] = bool(readonly)
        containers.extend(init_containers)
        volumes.append({"name": volume_name, "host": {}})

        mount_points.append({
            "sourceVolume": volume_name,
            "containerPath": secrets_files_path
        })
        depends_on.append({"containerName": init_name, "condition": "SUCCESS"})

    for dir_path in sidecar.get('writable_dirs') or []:
        volume_name = volume_name_for_writable_dir(dir_path, name)
        volumes.append({"name": volume_name, "host": {}})
        mount_points.append({
            "sourceVolume": volume_name,
            "containerPath": dir_path
        })

    if mount_points:
        container["mountPoints"] = mount_points
    if depends_on:
        container["dependsOn"] = depends_on

    containers.append(container)
    logger.info(f"Built sidecar container '{name}'")
    return containers, volumes

def generate_task_definition(config_dict=None, yaml_file_path=None, cluster_name=None, aws_region=None, registry=None, container_registry=None, image_name=None, tag=None, service_name=None, public_image=None):
    """
    Generate an ECS task definition from a simplified YAML configuration
    
    Args:
        config_dict (dict): Pre-loaded configuration dictionary
        yaml_file_path (str): Path to the YAML configuration file (if config_dict not provided)
        cluster_name (str): ECS cluster name
        aws_region (str): AWS region to use for log configuration
        registry (str): ECR registry URL for sidecars (OTEL/Fluent Bit)
        container_registry (str): ECR registry URL for main container
        image_name (str): Image name
        tag (str): Image tag
        service_name (str): ECS service name
    
    Returns:
        dict: The generated task definition
    """ 
    # Load config if not provided
    if config_dict is None:
        if yaml_file_path is None:
            raise ValidationError("Either config_dict or yaml_file_path must be provided")
        config = load_and_validate_config(yaml_file_path, service_name)
    else:
        config = config_dict

    # Cheap and idempotent, and the config_dict path above never went through
    # load_and_validate_config - without this a hand-built config could emit a
    # task definition with duplicate container or volume names.
    validate_sidecars(config)

    # Extract values from config
    # Use service name from action instead of YAML name
    app_name = service_name if service_name else config.get('name', 'app')
    cpu = str(config.get('cpu', 256))
    memory = str(config.get('memory', 512))
    # OTEL Collector block (new format)
    otel_collector = config.get('otel_collector')
    if otel_collector is not None:
        otel_collector_image_name = otel_collector.get('image_name', '').strip()
        otel_collector_ssm = otel_collector.get('ssm_name', 'adot-config-global.yaml').strip()
        otel_extra_config = otel_collector.get('extra_config', '').strip()
        otel_metrics_port = otel_collector.get('metrics_port', 8080)  # Default to 8080
        otel_metrics_path = otel_collector.get('metrics_path', '/metrics')  # Default to /metrics
        otel_is_custom_image = bool(otel_collector_image_name)
        if not otel_collector_image_name:
            otel_collector_image = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
        else:
            # Custom image name - ALWAYS use ECR registry (private image)
            logger.debug(f"registry='{registry}', otel_collector_image_name='{otel_collector_image_name}'")
            # Registry is always available for OTEL/Fluent Bit
            otel_collector_image = f"{registry}/{otel_collector_image_name}"
            logger.debug(f"Using ECR registry - otel_collector_image='{otel_collector_image}'")
    else:
        otel_collector_image = None
        otel_is_custom_image = False
    cpu_arch = config.get('cpu_arch', 'X86_64')
    command = config.get('command', [])
    entrypoint = config.get('entrypoint', [])
    health = build_health_check(config)

    # Extract replica_count for later use in the GitHub Action
    replica_count = config.get('replica_count', '')

    # Extract fluent_bit_collector config if present
    fluent_bit_collector = config.get('fluent_bit_collector', {})
    use_fluent_bit = bool(fluent_bit_collector and fluent_bit_collector.get('image_name', '').strip())
    config_name = fluent_bit_collector.get('extra_config', "extra.conf")
    ecs_log_metadata = fluent_bit_collector.get('ecs_log_metadata', 'true')
    extra_config = f"extra/{config_name}"
    # Handle fluent-bit image - ALWAYS ECR if image_name is specified
    if use_fluent_bit:
        fluent_bit_image_name = fluent_bit_collector.get('image_name', '').strip()
        # Registry is always available for OTEL/Fluent Bit
        fluent_bit_image = f"{registry}/{fluent_bit_image_name}"
    else:
        fluent_bit_image = ''
    
    # Get environment variables (changed from env_variables to envs)
    environment = build_environment(config)

    # Get secrets using the SecretManager
    secrets = SecretManager.build_secrets_from_config(config)
    
    # Check for secret_files configuration (multiple files now supported)
    secret_files = config.get('secret_files', [])
    has_secret_files = len(secret_files) > 0
    
    # Get configurable secrets files path (defaults to /etc/secrets)
    secrets_files_path = config.get('secrets_files_path', '/etc/secrets')
    
    # Create shared volume for secret files if needed
    volumes = []
    if has_secret_files:
        volumes.append({
            "name": "shared-volume",
            "host": {}
        })
    
    # Create volumes for writable directories (needed when readonlyRootFilesystem is true)
    writable_dirs = config.get('writable_dirs', [])
    for dir_path in writable_dirs:
        # Generate volume name from path: /tmp -> writable-tmp, /var/run -> writable-var-run
        vol_name = volume_name_for_writable_dir(dir_path)
        volumes.append({
            "name": vol_name,
            "host": {}
        })

    # Sanitize image_name and tag for ECR URI
    image_name_clean, tag_clean = parse_image_parts(image_name, tag)
    image_uri = build_image_uri(container_registry, image_name_clean, tag_clean)
    
    logger.info(f"Setting container image to: {image_uri}")
    
    # Get launch type and network mode (defaults for backwards compatibility)
    launch_type = config.get('launch_type', 'FARGATE').upper()
    network_mode = config.get('network_mode', 'awsvpc').lower()
    
    logger.info(f"Launch type: {launch_type}, Network mode: {network_mode}")
    
    # Create the container definitions list
    container_definitions = []
    
    # Create init containers for secret files if needed
    init_containers = build_init_containers(config, secret_files, cluster_name, app_name, aws_region, secrets_files_path)
    container_definitions.extend(init_containers)

    # Add the main application container
    app_container = build_app_container(config, image_uri, environment, secrets, health, cluster_name, app_name, aws_region, use_fluent_bit, has_secret_files, secrets_files_path, network_mode, launch_type)
    container_definitions.append(app_container)

    # Add fluent-bit sidecar container if enabled
    if use_fluent_bit:
        fluent_bit_container = build_fluent_bit_container(config, fluent_bit_image, app_name, cluster_name, aws_region)
        container_definitions.append(fluent_bit_container)
    
    # Add the OpenTelemetry collector container if enabled (new format)
    if otel_collector_image is not None:
        otel_container = build_otel_container(config, otel_collector_image, otel_is_custom_image, otel_collector_ssm, otel_extra_config, otel_metrics_port, otel_metrics_path, app_name, cluster_name, aws_region)
        container_definitions.append(otel_container)
    
    # The two passes below apply the application-level defaults to the
    # application container and its built-in companions (init, fluent-bit,
    # otel). Generic sidecars are deliberately appended AFTERWARDS: they are
    # isolated by contract and manage their own readonly flag and mounts.
    #
    # Apply readonlyRootFilesystem to the application containers if specified
    readonly_root_filesystem = config.get('readonly_root_filesystem')
    if readonly_root_filesystem is not None:
        for container in container_definitions:
            container["readonlyRootFilesystem"] = bool(readonly_root_filesystem)

    # Add writable_dirs mountPoints to the application containers if specified
    if writable_dirs:
        for container in container_definitions:
            if "mountPoints" not in container:
                container["mountPoints"] = []
            for dir_path in writable_dirs:
                vol_name = volume_name_for_writable_dir(dir_path)
                container["mountPoints"].append({
                    "sourceVolume": vol_name,
                    "containerPath": dir_path
                })

    # Generic sidecars, in declaration order. Must stay below the two blanket
    # passes above - that is what keeps a sidecar from inheriting application
    # mounts and the application readonly flag it explicitly overrode.
    for sidecar in enabled_sidecars(config):
        sidecar_containers, sidecar_volumes = build_sidecar_container(
            sidecar, config, cluster_name, app_name, aws_region,
            network_mode, launch_type
        )
        container_definitions.extend(sidecar_containers)
        volumes.extend(sidecar_volumes)

    # Resolve the two IAM role slots: YAML first, then the SSM parameters
    # published by terraform-aws-ecs-service. Unresolved slots are a hard error.
    # Done here, after the containers are built, so pure-config errors still
    # surface before any network I/O.
    role_arns = RoleResolver(cluster_name, app_name, aws_region).resolve(config)

    # Create the complete task definition
    task_definition = {
        "containerDefinitions": container_definitions,
        "cpu": cpu,
        "memory": memory,
        "family": f"{cluster_name}_{app_name}",
        **role_arns,
        "networkMode": network_mode,
        "requiresCompatibilities": [
            launch_type
        ]
    }
    
    # Add runtimePlatform only for Fargate (required for Fargate, not needed for EC2)
    if launch_type == 'FARGATE':
        task_definition["runtimePlatform"] = {
            "cpuArchitecture": cpu_arch,
            "operatingSystemFamily": "LINUX"
        }
    
    # Add ephemeral storage if specified
    ephemeral_storage = config.get('ephemeral_storage')
    if ephemeral_storage is not None:
        task_definition["ephemeralStorage"] = {
            "sizeInGiB": int(ephemeral_storage)
        }
        logger.info(f"Set ephemeral storage size to {ephemeral_storage} GiB")
    
    # Add volumes if needed
    if volumes:
        task_definition["volumes"] = volumes
    
    return task_definition

def parse_args():
    """Parse and validate command line arguments with better help"""
    parser = argparse.ArgumentParser(
        description='Generate ECS task definition from YAML configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s config.yaml my-cluster us-east-1 123456789.dkr.ecr.us-east-1.amazonaws.com \\
    123456789.dkr.ecr.us-east-1.amazonaws.com my-app latest my-service

  %(prog)s config.yaml my-cluster us-east-1 --output custom-task-def.json
        """
    )
    
    parser.add_argument('yaml_file', 
                       help='Path to the YAML configuration file')
    parser.add_argument('cluster_name', 
                       help='ECS cluster name')
    parser.add_argument('aws_region', 
                       help='AWS region for log configuration')
    parser.add_argument('registry', 
                       help='ECR registry URL for sidecars (OTEL/Fluent Bit)')
    parser.add_argument('container_registry', 
                       help='ECR registry URL for main container')
    parser.add_argument('image_name', 
                       help='Container image name')
    parser.add_argument('tag', 
                       help='Container image tag')
    parser.add_argument('service_name', 
                       help='ECS service name')
    parser.add_argument('--output', '-o', 
                       default='task-definition.json',
                       help='Output file path (default: %(default)s)')
    parser.add_argument('--log-level', 
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       default='INFO',
                       help='Logging level (default: %(default)s)')
    parser.add_argument('--validate-only', 
                       action='store_true',
                       help='Only validate configuration, do not generate output')
    
    args = parser.parse_args()
    
    # Validate arguments
    if not Path(args.yaml_file).exists():
        parser.error(f"YAML file does not exist: {args.yaml_file}")
    
    return args

def emit_replica_count(replica_count: Any) -> None:
    """Publish replica_count as a GitHub Actions step output.

    Feeds `desired-count` on the deploy step. An unset replica_count writes an
    empty value, which amazon-ecs-deploy-task-definition treats as "not
    specified", leaving the service's live desired count alone.
    """
    value = '' if replica_count is None else str(replica_count).strip()

    if value:
        # Guard against a typo like `replica_count: two` becoming an opaque
        # failure inside the deploy action. Deliberately stricter than int():
        # that accepts "5_0" as 50 and non-ASCII digits that GitHub Actions
        # would then hand to the deploy step as garbage. Zero is valid - it is
        # how a service is scaled down.
        if re.fullmatch(r'[0-9]+', value):
            value = str(int(value))  # normalize e.g. "007" -> "7"
        else:
            logger.warning(
                f"Ignoring replica_count={value!r}: expected a non-negative integer. "
                f"The service's current desired count will be left unchanged."
            )
            value = ''

    github_output = os.environ.get('GITHUB_OUTPUT')
    if not github_output:
        logger.debug(
            f"GITHUB_OUTPUT not set (not running in GitHub Actions); "
            f"replica_count={value!r}"
        )
        return

    with open(github_output, 'a', encoding='utf-8') as handle:
        handle.write(f"replica_count={value}\n")
    logger.info(f"Set GitHub Actions output replica_count={value!r}")

def main() -> None:
    """Main function with proper error handling"""
    try:
        args = parse_args()
        
        # Setup logging
        global logger
        logger = setup_logging(args.log_level)
        
        # Load and validate configuration
        config = load_and_validate_config(args.yaml_file, args.service_name)
        
        if args.validate_only:
            logger.info("Configuration validation successful")
            return
        
        # Generate task definition
        task_definition = generate_task_definition(
            config_dict=config,
            cluster_name=args.cluster_name,
            aws_region=args.aws_region,
            registry=args.registry,
            container_registry=args.container_registry,
            image_name=args.image_name,
            tag=args.tag,
            service_name=args.service_name
        )
        
        # Write output
        output_path = Path(args.output)
        with output_path.open('w') as file:
            json.dump(task_definition, file, indent=2)
        
        logger.info(f"Task definition written to {output_path}")
        
        # Output for GitHub Actions. ::set-output was disabled by GitHub in 2023
        # (and was being written to stderr besides), so this output never
        # actually reached the workflow; $GITHUB_OUTPUT is the supported way.
        emit_replica_count(config.get('replica_count', ''))

        # Output JSON to stdout for tests and compatibility
        print(json.dumps(task_definition, indent=2))

    except RoleResolutionError as e:
        logger.error(f"Role resolution failed:\n{e}")
        sys.exit(1)
    except ValidationError as e:
        logger.error(f"Configuration validation failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if logger.level == logging.DEBUG:
            logger.exception("Full traceback:")
        sys.exit(1)

if __name__ == "__main__":
    main()
