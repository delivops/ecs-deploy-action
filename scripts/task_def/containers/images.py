import logging
from typing import Optional

logger = logging.getLogger(__name__)


def parse_image_parts(image_name: str, tag: str) -> tuple[str, str]:
    """Parse and clean image name and tag"""
    logger.debug(f"Parsing image parts: image_name='{image_name}', tag='{tag}'")

    # Remove registry if mistakenly included in image_name
    if "/" in image_name and "." in image_name.split("/")[0]:
        # Remove registry part
        image_name = "/".join(image_name.split("/")[1:])
        logger.debug(f"Removed registry from image_name: '{image_name}'")

    # Remove tag from image_name if present
    if ":" in image_name:
        image_name, image_tag = image_name.split(":", 1)
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
