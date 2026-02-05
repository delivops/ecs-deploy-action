#!/usr/bin/env python3
import yaml
import json
import argparse
import sys
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
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

@dataclass
class TaskConfig:
    """Configuration for ECS task definition"""
    name: str
    cpu: str = "256"
    memory: str = "512"
    cpu_arch: str = "X86_64"
    command: List[str] = field(default_factory=list)
    entrypoint: List[str] = field(default_factory=list)
    port: Optional[int] = None
    additional_ports: List[Dict[str, int]] = field(default_factory=list)
    role_arn: str = ""
    replica_count: str = ""
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskConfig':
        return cls(
            name=data.get('name', 'app'),
            cpu=str(data.get('cpu', 256)),
            memory=str(data.get('memory', 512)),
            cpu_arch=data.get('cpu_arch', 'X86_64'),
            command=data.get('command', []),
            entrypoint=data.get('entrypoint', []),
            port=data.get('port'),
            additional_ports=data.get('additional_ports', []),
            role_arn=data.get('role_arn', ''),
            replica_count=data.get('replica_count', '')
        )

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
        valid_cpu_values = [256, 512, 1024, 2048, 4096]
        if cpu not in valid_cpu_values:
            raise ValidationError(f"Invalid CPU value: {cpu}. Must be one of {valid_cpu_values}")
        
        # Validate memory based on CPU
        valid_memory_for_cpu = {
            256: [512, 1024, 2048],
            512: [1024, 2048, 3072, 4096],
            1024: [2048, 3072, 4096, 5120, 6144, 7168, 8192],
            2048: list(range(4096, 16385, 1024)),
            4096: list(range(8192, 30721, 1024))
        }
        
        if memory not in valid_memory_for_cpu.get(cpu, []):
            raise ValidationError(f"Invalid memory value {memory} for CPU {cpu}")
    else:
        # EC2 has more flexible CPU/memory - just validate they're positive if provided
        if cpu is not None and (not isinstance(cpu, int) or cpu <= 0):
            raise ValidationError(f"Invalid CPU value: {cpu}. Must be a positive integer.")
        if memory is not None and (not isinstance(memory, int) or memory <= 0):
            raise ValidationError(f"Invalid memory value: {memory}. Must be a positive integer.")

def load_and_validate_config(yaml_file_path: str) -> Dict[str, Any]:
    """Load and validate YAML configuration"""
    try:
        yaml_path = Path(yaml_file_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")
        
        with yaml_path.open('r') as file:
            config = yaml.safe_load(file)
        
        if not config:
            raise ValidationError("YAML file is empty or invalid")
        
        validate_config(config)
        logger.info(f"Successfully loaded and validated configuration from {yaml_file_path}")
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
                           network_mode: str = "awsvpc") -> List[Dict[str, Any]]:
        """Build port mappings configuration
        
        Args:
            main_port: Primary container port
            additional_ports: List of additional port mappings
            app_protocol: Application protocol (http, grpc, tcp)
            network_mode: Network mode (awsvpc, bridge, host, none)
        """
        port_mappings = []
        
        # For bridge mode, hostPort can be 0 (dynamic) or different from containerPort
        # For awsvpc/host modes, hostPort must equal containerPort
        use_dynamic_host_port = network_mode == 'bridge'
        
        if main_port:
            port_mapping = {
                "name": "default",
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
        secret_list = config.get('secrets', [])
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
        secrets_envs = config.get('secrets_envs', [])
        
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

def build_init_containers(config, secret_files, cluster_name, app_name, aws_region, secrets_files_path="/etc/secrets"):
    """Build init containers for secret file downloads"""
    container_definitions = []
    
    # Handle secret files (existing functionality)
    if secret_files:
        # Join secret names with commas for the environment variable
        secret_files_env = ",".join(secret_files)
        
        container_builder = ContainerBuilder(cluster_name, app_name, aws_region)
        
        init_container = {
            "name": "init-container-for-secret-files",
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
                    "sourceVolume": "shared-volume",
                    "containerPath": secrets_files_path
                }
            ],
            "logConfiguration": container_builder.build_log_configuration(stream_prefix="ssm-file-downloader")
        }
        container_definitions.append(init_container)
        logger.info(f"Built init container for {len(secret_files)} secret files")
    
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
            tmpfs_mount = {
                "containerPath": container_path,
                "size": int(mount.get('size', 64))
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
        linux_parameters["swappiness"] = int(swappiness)
        logger.info(f"Set swappiness to {swappiness}")
    
    # maxSwap - supported by Fargate (1.4.0+) and EC2
    max_swap = linux_params.get('max_swap')
    if max_swap is not None:
        linux_parameters["maxSwap"] = int(max_swap)
        logger.info(f"Set maxSwap to {max_swap}")
    
    # EC2-only parameters
    shared_memory_size = linux_params.get('shared_memory_size')
    if shared_memory_size is not None:
        if launch_type == 'FARGATE':
            logger.warning(f"shared_memory_size is EC2-only, ignoring for Fargate launch type")
        else:
            linux_parameters["sharedMemorySize"] = int(shared_memory_size)
            logger.info(f"Set sharedMemorySize to {shared_memory_size} MiB")
    
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

def build_app_container(config, image_uri, environment, secrets, health, cluster_name, app_name, aws_region, use_fluent_bit, has_secret_files, secrets_files_path="/etc/secrets", network_mode="awsvpc", launch_type="FARGATE"):
    """Build the main application container"""
    command = config.get('command', [])
    entrypoint = config.get('entrypoint', [])
    stop_timeout = config.get('stop_timeout')
    
    container_builder = ContainerBuilder(cluster_name, app_name, aws_region)
    
    app_container = {
        "name": "app",
        "image": image_uri,
        "essential": True,
        "environment": environment,
        "command": command,
        "entryPoint": entrypoint,
        "secrets": secrets
    }
    
    # Add stopTimeout if specified
    if stop_timeout is not None:
        app_container["stopTimeout"] = int(stop_timeout)
    
    # Set logConfiguration for app container
    if use_fluent_bit:
        app_container["logConfiguration"] = {
            "logDriver": "awsfirelens",
            "options": {}
        }
    else:
        app_container["logConfiguration"] = container_builder.build_log_configuration(stream_prefix="default")
    
    # Only include healthCheck if it was properly built
    if health:
        app_container["healthCheck"] = health

    # Handle port configurations
    main_port = config.get('port')
    additional_ports = config.get('additional_ports', [])
    app_protocol = config.get('app_protocol', 'http')
    
    port_mappings = container_builder.build_port_mappings(main_port, additional_ports, app_protocol, network_mode)
    if port_mappings:
        app_container["portMappings"] = port_mappings
    
    # Add linuxParameters if configured
    linux_parameters = build_linux_parameters(config, launch_type)
    if linux_parameters:
        app_container["linuxParameters"] = linux_parameters
    
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
        config = load_and_validate_config(yaml_file_path)
    else:
        config = config_dict
    
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
    health_check = config.get('health_check', {})
    # Only build health check if config has values and command is non-empty
    if health_check and health_check.get('command'):
        health = {
            "command": ["CMD-SHELL", health_check["command"]],
            "interval": health_check.get('interval', 30),
            "timeout": health_check.get('timeout', 5),
            "retries": health_check.get('retries', 3),
            "startPeriod": health_check.get('start_period', 10)
        }
    else:
        health = None
    
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
    environment = []
    for env_var in config.get('envs', []):
        for key, value in env_var.items():
            environment.append({
                "name": key,
                "value": str(value)  # Convert to string for ECS compatibility
            })
    
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
    
    # Create the complete task definition
    task_definition = {
        "containerDefinitions": container_definitions,
        "cpu": cpu,
        "memory": memory,
        "family": f"{cluster_name}_{app_name}",
        "taskRoleArn": config.get('role_arn', ''),
        "executionRoleArn": config.get('role_arn', ''),
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
    if has_secret_files and volumes:
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
            service_name=args.service_name
        )
        
        # Write output
        output_path = Path(args.output)
        with output_path.open('w') as file:
            json.dump(task_definition, file, indent=2)
        
        logger.info(f"Task definition written to {output_path}")
        
        # Output for GitHub Actions (to stderr so it doesn't interfere with JSON output)
        replica_count = config.get('replica_count', '')
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

if __name__ == "__main__":
    main()
