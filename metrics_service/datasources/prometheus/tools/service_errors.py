"""
datasources/prometheus/tools/service_errors.py -- get_service_error_rate(service, namespace, range, source_override)
"""

import logging
from typing import Any

from ....config import get_settings, validate_label, validate_range
from ..client import query_instant
from ..registry import get_registry

logger = logging.getLogger(__name__)

METRIC_NAME = "service_errors"


def get_service_error_rate(service, namespace, range="1h", source_override=None):
    settings = get_settings()
    service = validate_label(service, "service")
    namespace = validate_label(namespace, "namespace")
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric(METRIC_NAME, source_override)

    total_promql = (
        f'sum(rate(http_requests_total'
        f'{{service="{service}",namespace="{namespace}"}}[{range}]))'
    )
    error_promql = (
        f'sum(rate(http_requests_total'
        f'{{service="{service}",namespace="{namespace}",status=~"5.."}}[{range}]))'
    )

    total_rps = None
    error_rps = None

    try:
        total_results = query_instant(total_promql, source)
        if total_results:
            total_rps = float(total_results[0]["value"][1])
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve total RPS. service=%s source=%s", service, source.name)

    try:
        error_results = query_instant(error_promql, source)
        if error_results:
            error_rps = float(error_results[0]["value"][1])
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve error RPS. service=%s source=%s", service, source.name)

    error_rate_pct = None
    if total_rps is not None and error_rps is not None and total_rps > 0:
        error_rate_pct = round((error_rps / total_rps) * 100, 2)
    elif total_rps == 0:
        error_rate_pct = 0.0

    return {
        "service": service,
        "namespace": namespace,
        "range": range,
        "source": source.safe_info(),
        "error_rate_percent": error_rate_pct,
        "total_requests_per_second": round(total_rps, 4) if total_rps is not None else None,
        "error_requests_per_second": round(error_rps, 4) if error_rps is not None else None,
    }
