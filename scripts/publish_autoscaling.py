#!/usr/bin/env python3
"""
Publish autoscaling configurations to DynamoDB.

This script reads autoscaling_configs from the deployment YAML and publishes
it to DynamoDB table '${ecs_cluster}_ecs_autoscaling_config' with optimistic concurrency control.
"""

import json
import sys
import logging
import hashlib
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import argparse

import yaml
import boto3
from botocore.exceptions import ClientError
from jsonschema import validate, ValidationError as JsonSchemaValidationError


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)


class AutoscalingPublishError(Exception):
    """Base exception for autoscaling publish errors"""
    pass


class ConfigValidationError(AutoscalingPublishError):
    """Configuration validation failed"""
    pass


def load_json_schema() -> Dict[str, Any]:
    """Load the JSON schema for autoscaling config validation"""
    # Find schema file relative to this script
    script_dir = Path(__file__).parent
    schema_path = script_dir.parent / "schemas" / "autoscaling.v1.json"
    
    try:
        with schema_path.open('r') as f:
            schema = json.load(f)
        logger.debug(f"Loaded JSON schema from {schema_path}")
        return schema
    except Exception as e:
        raise AutoscalingPublishError(f"Failed to load JSON schema: {e}")


def load_yaml_config(yaml_file_path: str) -> Dict[str, Any]:
    """Load YAML configuration file"""
    try:
        yaml_path = Path(yaml_file_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")
        
        with yaml_path.open('r') as file:
            config = yaml.safe_load(file)
        
        if not config:
            raise AutoscalingPublishError("YAML file is empty or invalid")
        
        logger.info(f"Successfully loaded configuration from {yaml_file_path}")
        return config
        
    except yaml.YAMLError as e:
        raise AutoscalingPublishError(f"Invalid YAML format: {e}")


def extract_autoscaling_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract autoscaling_configs block from YAML config"""
    autoscaling_configs = config.get('autoscaling_configs')
    
    if autoscaling_configs is None:
        logger.info("No autoscaling_configs block found in YAML - skipping publish")
        return None
    
    return autoscaling_configs


def validate_autoscaling_config(config: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Validate autoscaling config against JSON schema"""
    try:
        # Inject version (hardcoded as v1)
        config_with_version = {**config, "version": 1}
        
        # Validate using jsonschema
        validate(instance=config, schema=schema)
        
        # Additional custom validations
        _validate_custom_rules(config)
        
        logger.info("Autoscaling config validation passed")
        
    except JsonSchemaValidationError as e:
        error_msg = f"Schema validation failed: {e.message}"
        if e.path:
            path_str = ".".join(str(p) for p in e.path)
            error_msg += f" at path: {path_str}"
        logger.error(error_msg)
        raise ConfigValidationError(error_msg)


def _validate_custom_rules(config: Dict[str, Any]) -> None:
    """Additional custom validation rules beyond JSON schema"""
    
    # Validate max_tasks >= min_tasks
    min_tasks = config.get('min_tasks', 0)
    max_tasks = config.get('max_tasks', 1)
    
    if max_tasks < min_tasks:
        raise ConfigValidationError(
            f"max_tasks ({max_tasks}) must be >= min_tasks ({min_tasks})"
        )
    
    # Validate time rules if present
    provider = config.get('provider', {})
    time_config = provider.get('time')
    
    if time_config:
        mode = time_config.get('mode')
        rules = time_config.get('rules', [])
        
        # If mode is 'override', at least one rule must have 'desired'
        if mode == 'override':
            has_desired = any('desired' in rule for rule in rules)
            if not has_desired:
                raise ConfigValidationError(
                    "time.mode='override' requires at least one rule with 'desired' field"
                )
        
        # Validate start < end for each rule with both fields
        for idx, rule in enumerate(rules):
            if 'start' in rule and 'end' in rule:
                start = rule['start']
                end = rule['end']
                if start >= end:
                    raise ConfigValidationError(
                        f"Rule {idx}: start time ({start}) must be before end time ({end})"
                    )
    
    # Validate scale_in_guard if present
    scale_in_guard = config.get('scale_in_guard')
    if scale_in_guard:
        mode = scale_in_guard.get('mode')
        if mode == 'low_latency':
            if 'age_below_seconds' not in scale_in_guard:
                raise ConfigValidationError(
                    "scale_in_guard with mode='low_latency' requires 'age_below_seconds'"
                )
            if 'visible_below' not in scale_in_guard:
                raise ConfigValidationError(
                    "scale_in_guard with mode='low_latency' requires 'visible_below'"
                )


def compute_checksum(config: Dict[str, Any]) -> str:
    """Compute SHA256 checksum of canonical JSON representation"""
    # Use sorted keys for canonical representation
    canonical_json = json.dumps(config, sort_keys=True, separators=(',', ':'))
    checksum = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    logger.debug(f"Computed checksum: {checksum}")
    return checksum


def build_service_key(environment: str, ecs_cluster: str, ecs_service: str) -> str:
    """Build service_key for DynamoDB primary key"""
    service_key = f"{environment}:{ecs_cluster}:{ecs_service}"
    logger.debug(f"Built service_key: {service_key}")
    return service_key


def publish_to_dynamodb(
    config: Dict[str, Any],
    service_key: str,
    environment: str,
    ecs_cluster: str,
    aws_region: str,
    commit_sha: Optional[str] = None
) -> Tuple[bool, str, str, int]:
    """
    Publish autoscaling config to DynamoDB with optimistic concurrency.
    
    Returns:
        Tuple of (success, checksum, service_key, updated_at)
    """
    table_name = f"{ecs_cluster}_ecs_autoscaling_config"
    
    # Inject version=1
    config_with_version = {**config, "version": 1}
    
    # Compute checksum
    checksum = compute_checksum(config_with_version)
    
    # Get current timestamp
    updated_at = int(time.time())
    
    # Get commit SHA from environment if not provided
    if commit_sha is None:
        commit_sha = os.environ.get('GITHUB_SHA', 'unknown')
    
    # Build DynamoDB item
    item = {
        'service_key': {'S': service_key},
        'env': {'S': environment},
        'version': {'N': '1'},
        'config': {'M': _convert_to_dynamodb_format(config_with_version)},
        'checksum': {'S': checksum},
        'commit_sha': {'S': commit_sha},
        'updated_at': {'N': str(updated_at)}
    }
    
    try:
        # Create DynamoDB client
        dynamodb = boto3.client('dynamodb', region_name=aws_region)
        
        # Check if table exists
        try:
            dynamodb.describe_table(TableName=table_name)
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                logger.warning(f"Table '{table_name}' does not exist - skipping publish")
                return False, checksum, service_key, updated_at
            raise
        
        # Put item with condition expression (optimistic concurrency)
        try:
            dynamodb.put_item(
                TableName=table_name,
                Item=item,
                ConditionExpression='attribute_not_exists(service_key) OR updated_at <= :now',
                ExpressionAttributeValues={
                    ':now': {'N': str(updated_at)}
                }
            )
            
            logger.info(
                f"✅ Published autoscaling_configs for {service_key} "
                f"(checksum: {checksum[:12]}...)"
            )
            return True, checksum, service_key, updated_at
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                logger.warning(
                    f"⚠️  Skipped publish for {service_key}: "
                    f"existing config is newer (conditional check failed)"
                )
                return False, checksum, service_key, updated_at
            raise
    
    except Exception as e:
        logger.error(f"Failed to publish to DynamoDB: {e}")
        raise AutoscalingPublishError(f"DynamoDB publish failed: {e}")


def _convert_to_dynamodb_format(obj: Any) -> Dict[str, Any]:
    """Convert Python object to DynamoDB attribute format"""
    if obj is None:
        return {'NULL': True}
    elif isinstance(obj, bool):
        return {'BOOL': obj}
    elif isinstance(obj, (int, float)):
        return {'N': str(obj)}
    elif isinstance(obj, str):
        return {'S': obj}
    elif isinstance(obj, list):
        return {'L': [_convert_to_dynamodb_format(item) for item in obj]}
    elif isinstance(obj, dict):
        return {'M': {k: _convert_to_dynamodb_format(v) for k, v in obj.items()}}
    else:
        # Fallback to string representation
        return {'S': str(obj)}


def set_github_output(name: str, value: str) -> None:
    """Set GitHub Actions output"""
    # GitHub Actions new format (via GITHUB_OUTPUT file)
    github_output = os.environ.get('GITHUB_OUTPUT')
    if github_output:
        with open(github_output, 'a') as f:
            f.write(f"{name}={value}\n")
    else:
        # Fallback to old format
        print(f"::set-output name={name}::{value}", file=sys.stderr)


def main() -> None:
    """Main function"""
    parser = argparse.ArgumentParser(
        description='Publish autoscaling configuration to DynamoDB'
    )
    parser.add_argument('task_config_yaml', help='Path to task configuration YAML')
    parser.add_argument('environment', help='Deployment environment')
    parser.add_argument('ecs_cluster', help='ECS cluster name')
    parser.add_argument('ecs_service', help='ECS service name')
    parser.add_argument('aws_region', help='AWS region')
    parser.add_argument('--commit-sha', help='Git commit SHA (defaults to GITHUB_SHA env var)')
    parser.add_argument('--log-level', 
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       default='INFO',
                       help='Logging level')
    
    args = parser.parse_args()
    
    # Set log level
    logger.setLevel(getattr(logging, args.log_level.upper()))
    
    try:
        # Load YAML config
        config = load_yaml_config(args.task_config_yaml)
        
        # Extract autoscaling_configs block
        autoscaling_config = extract_autoscaling_config(config)
        
        if autoscaling_config is None:
            # No autoscaling config - set outputs and exit successfully
            set_github_output('autoscaler_published', 'false')
            set_github_output('autoscaler_service_key', '')
            set_github_output('autoscaler_checksum', '')
            set_github_output('autoscaler_updated_at', '')
            logger.info("No autoscaling config found - nothing to publish")
            return
        
        # Load and validate against JSON schema
        schema = load_json_schema()
        validate_autoscaling_config(autoscaling_config, schema)
        
        # Build service key
        service_key = build_service_key(
            args.environment,
            args.ecs_cluster,
            args.ecs_service
        )
        
        # Publish to DynamoDB
        success, checksum, service_key, updated_at = publish_to_dynamodb(
            autoscaling_config,
            service_key,
            args.environment,
            args.ecs_cluster,
            args.aws_region,
            args.commit_sha
        )
        
        # Set GitHub Actions outputs
        set_github_output('autoscaler_published', str(success).lower())
        set_github_output('autoscaler_service_key', service_key)
        set_github_output('autoscaler_checksum', checksum)
        set_github_output('autoscaler_updated_at', str(updated_at))
        
        logger.info("Autoscaling publish completed successfully")
        
    except ConfigValidationError as e:
        logger.error(f"❌ Autoscaling config validation failed:")
        logger.error(f"   {e}")
        logger.error("   Autoscaling config not published; deploy continues.")
        
        # Set outputs indicating failure
        set_github_output('autoscaler_published', 'false')
        set_github_output('autoscaler_service_key', '')
        set_github_output('autoscaler_checksum', '')
        set_github_output('autoscaler_updated_at', '')
        
        # Don't fail the deploy - just continue
        sys.exit(0)
        
    except AutoscalingPublishError as e:
        logger.error(f"❌ Failed to publish autoscaling config: {e}")
        logger.error("   Autoscaling config not published; deploy continues.")
        
        # Set outputs indicating failure
        set_github_output('autoscaler_published', 'false')
        set_github_output('autoscaler_service_key', '')
        set_github_output('autoscaler_checksum', '')
        set_github_output('autoscaler_updated_at', '')
        
        # Don't fail the deploy
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}")
        logger.exception("Full traceback:")
        logger.error("   Autoscaling config not published; deploy continues.")
        
        # Set outputs indicating failure
        set_github_output('autoscaler_published', 'false')
        set_github_output('autoscaler_service_key', '')
        set_github_output('autoscaler_checksum', '')
        set_github_output('autoscaler_updated_at', '')
        
        # Don't fail the deploy
        sys.exit(0)


if __name__ == "__main__":
    main()

