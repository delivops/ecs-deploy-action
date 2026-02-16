from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ValidationError(Exception):
    """Custom exception for validation errors"""


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
    def from_dict(cls, data: Dict[str, Any]) -> "TaskConfig":
        return cls(
            name=data.get("name", "app"),
            cpu=str(data.get("cpu", 256)),
            memory=str(data.get("memory", 512)),
            cpu_arch=data.get("cpu_arch", "X86_64"),
            command=data.get("command", []),
            entrypoint=data.get("entrypoint", []),
            port=data.get("port"),
            additional_ports=data.get("additional_ports", []),
            role_arn=data.get("role_arn", ""),
            replica_count=data.get("replica_count", ""),
        )
