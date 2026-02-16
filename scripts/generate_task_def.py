#!/usr/bin/env python3
"""Compatibility wrapper for task definition generation.

This module re-exports the public API while delegating implementation to
`scripts/task_def/` modules.
"""

from task_def import (  # noqa: F401
    ContainerBuilder,
    LogLevel,
    SecretManager,
    TaskConfig,
    ValidationError,
    build_app_container,
    build_fluent_bit_container,
    build_image_uri,
    build_init_containers,
    build_linux_parameters,
    build_otel_container,
    generate_task_definition,
    load_and_validate_config,
    parse_args,
    parse_image_parts,
    setup_logging,
    validate_config,
)
from task_def.cli import main


if __name__ == "__main__":
    main()
