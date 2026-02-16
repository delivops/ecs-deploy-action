import logging

from .base import ContainerBuilder

logger = logging.getLogger(__name__)


def build_init_containers(
    config, secret_files, cluster_name, app_name, aws_region, secrets_files_path="/etc/secrets"
):
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
                f"done",
            ],
            "environment": [
                {
                    "name": "SECRET_FILES",
                    "value": secret_files_env,
                },
                {
                    "name": "AWS_REGION",
                    "value": aws_region,
                },
            ],
            "mountPoints": [
                {
                    "sourceVolume": "shared-volume",
                    "containerPath": secrets_files_path,
                }
            ],
            "logConfiguration": container_builder.build_log_configuration(
                stream_prefix="ssm-file-downloader"
            ),
        }
        container_definitions.append(init_container)
        logger.info(f"Built init container for {len(secret_files)} secret files")

    return container_definitions
