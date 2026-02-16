import argparse
import json
import logging
import sys
from pathlib import Path

from .common import ValidationError
from .config_loader import load_and_validate_config
from .generator import generate_task_definition
from .logging_utils import setup_logging


logger = setup_logging()


def parse_args():
    """Parse and validate command line arguments with better help"""
    parser = argparse.ArgumentParser(
        description="Generate ECS task definition from YAML configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s config.yaml my-cluster us-east-1 123456789.dkr.ecr.us-east-1.amazonaws.com \\
    123456789.dkr.ecr.us-east-1.amazonaws.com my-app latest my-service

  %(prog)s config.yaml my-cluster us-east-1 --output custom-task-def.json
        """,
    )

    parser.add_argument("yaml_file", help="Path to the YAML configuration file")
    parser.add_argument("cluster_name", help="ECS cluster name")
    parser.add_argument("aws_region", help="AWS region for log configuration")
    parser.add_argument("registry", help="ECR registry URL for sidecars (OTEL/Fluent Bit)")
    parser.add_argument("container_registry", help="ECR registry URL for main container")
    parser.add_argument("image_name", help="Container image name")
    parser.add_argument("tag", help="Container image tag")
    parser.add_argument("service_name", help="ECS service name")
    parser.add_argument(
        "--output",
        "-o",
        default="task-definition.json",
        help="Output file path (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: %(default)s)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate configuration, do not generate output",
    )

    args = parser.parse_args()

    # Validate arguments
    if not Path(args.yaml_file).exists():
        parser.error(f"YAML file does not exist: {args.yaml_file}")

    return args


def main() -> None:
    """Main function with proper error handling"""
    try:
        args = parse_args()

        # Setup logging
        global logger
        logger = setup_logging(args.log_level)

        # Load and validate configuration
        config = load_and_validate_config(args.yaml_file)

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
            service_name=args.service_name,
        )

        # Write output
        output_path = Path(args.output)
        with output_path.open("w") as file:
            json.dump(task_definition, file, indent=2)

        logger.info(f"Task definition written to {output_path}")

        # Output for GitHub Actions (to stderr so it doesn't interfere with JSON output)
        replica_count = config.get("replica_count", "")
        print(f"::set-output name=replica_count::{replica_count}", file=sys.stderr)

        # Output JSON to stdout for tests and compatibility
        print(json.dumps(task_definition, indent=2))

    except ValidationError as e:
        logger.error(f"Configuration validation failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if logger.level == logging.DEBUG:
            logger.exception("Full traceback:")
        sys.exit(1)
