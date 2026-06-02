from .prometheus.registry import get_registry
from .azure_monitor.registry import get_azure_registry

__all__ = ["get_registry", "get_azure_registry"]
