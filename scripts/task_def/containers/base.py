import logging
from typing import Any, Dict, List, Optional


class ContainerBuilder:
    """Builder class for container configurations"""

    def __init__(self, cluster_name: str, app_name: str, aws_region: str):
        self.cluster_name = cluster_name
        self.app_name = app_name
        self.aws_region = aws_region
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def build_log_configuration(
        self, log_driver: str = "awslogs", stream_prefix: str = "default"
    ) -> Dict[str, Any]:
        """Build standard log configuration"""
        # Add leading slash only for "default" stream prefix for compatibility
        if stream_prefix == "default":
            stream_prefix = "/default"

        return {
            "logDriver": log_driver,
            "options": {
                "awslogs-group": f"/ecs/{self.cluster_name}/{self.app_name}",
                "awslogs-region": self.aws_region,
                "awslogs-stream-prefix": stream_prefix,
            },
        }

    def build_port_mappings(
        self,
        main_port: Optional[int],
        additional_ports: List[Dict[str, int]],
        app_protocol: str = "http",
        network_mode: str = "awsvpc",
    ) -> List[Dict[str, Any]]:
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
        use_dynamic_host_port = network_mode == "bridge"

        if main_port:
            port_mapping = {
                "name": "default",
                "containerPort": main_port,
                "hostPort": 0 if use_dynamic_host_port else main_port,
                "protocol": "tcp",
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
                        "protocol": "tcp",
                    }
                    if app_protocol != "tcp":
                        port_mapping["appProtocol"] = app_protocol
                    port_mappings.append(port_mapping)

        self.logger.debug(f"Built {len(port_mappings)} port mappings (network_mode={network_mode})")
        return port_mappings
