"""
report_formatter.py — Rule-based daily production health report formatter.

No AI calls. Thresholds drive ✅ / ⚠️ / 🔴 status per resource.
Entry point: format_daily_report(aks_data, azure_data, report_date)
"""

from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_APP_MEM_WARN  = 500 * 1024 * 1024   # 500 MB
_APP_MEM_CRIT  = 800 * 1024 * 1024   # 800 MB
_APP_ERR_WARN  = 5.0
_APP_ERR_CRIT  = 10.0

_DB_CPU_WARN   = 70.0
_DB_CPU_CRIT   = 90.0
_DB_MEM_WARN   = 80.0
_DB_MEM_CRIT   = 90.0
_DB_IO_WARN    = 80.0

_REDIS_MEM_WARN  = 75.0
_REDIS_MEM_CRIT  = 90.0
_REDIS_LOAD_WARN = 75.0

_SB_DEAD_WARN   = 1
_SB_DEAD_CRIT   = 10
_SB_ACTIVE_WARN = 1000

_AKS_CPU_WARN  = 70.0
_AKS_CPU_CRIT  = 90.0
_AKS_MEM_WARN  = 80.0
_AKS_MEM_CRIT  = 90.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _icon(value: float | None, warn: float, crit: float | None = None) -> str:
    if value is None:
        return "—"
    if crit is not None and value >= crit:
        return "🔴"
    if value >= warn:
        return "⚠️"
    return "✅"


def _worst(*icons: str) -> str:
    """Return the most severe icon from a list."""
    for icon in ("🔴", "⚠️"):
        if icon in icons:
            return icon
    if all(i == "—" for i in icons):
        return "—"
    return "✅"


def _pct(v: float | None, decimals: int = 1) -> str:
    return f"{v:.{decimals}f}%" if v is not None else "—"


def _ms(v: float | None) -> str:
    return f"{v:.2f}ms" if v is not None else "—"


def _bytes_mb(v: float | None) -> str:
    if v is None:
        return "—"
    mb = v / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


def _num(v: float | int | None) -> str:
    if v is None:
        return "—"
    n = int(v)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _conn(v: float | None) -> str:
    return str(int(v)) if v is not None else "—"


# ---------------------------------------------------------------------------
# AKS section
# ---------------------------------------------------------------------------

def _format_aks_cluster(cluster: dict) -> list[str]:
    name   = cluster.get("cluster_name", "unknown")
    pools  = cluster.get("pools") or []
    error  = cluster.get("error")

    lines = [f"**☸️  {name}**"]

    if error or not pools:
        lines.append("  _(no data available)_")
        return lines

    for pool in pools:
        pname   = pool.get("pool_name", "unknown")
        total   = pool.get("node_count", 0)
        ready   = pool.get("ready_nodes", 0)
        avg_cpu = pool.get("avg_cpu_percent")
        max_cpu = pool.get("max_cpu_percent")
        avg_mem = pool.get("avg_memory_percent")
        max_mem = pool.get("max_memory_percent")

        node_ok  = "✅" if ready == total else ("⚠️" if ready > 0 else "🔴")
        cpu_icon = _icon(avg_cpu, _AKS_CPU_WARN, _AKS_CPU_CRIT)
        mem_icon = _icon(avg_mem, _AKS_MEM_WARN, _AKS_MEM_CRIT)

        cpu_str = f"{cpu_icon} {_pct(avg_cpu)} (max {_pct(max_cpu)})" if avg_cpu is not None else "—"
        mem_str = f"{mem_icon} {_pct(avg_mem)} (max {_pct(max_mem)})" if avg_mem is not None else "—"

        lines.append(
            f"  {node_ok} **{pname}** — {ready}/{total} nodes"
            f"  ·  CPU {cpu_str}  ·  Mem {mem_str}"
        )

    return lines


def format_aks_section(aks_data: dict) -> str:
    clusters = aks_data.get("clusters") or []
    if not clusters:
        return "**☸️  AKS**\n  _(no clusters configured)_"

    blocks = []
    for c in clusters:
        blocks.append("\n".join(_format_aks_cluster(c)))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Azure project sections
# ---------------------------------------------------------------------------

def _app_service_lines(items: list) -> list[str]:
    lines = ["*App Services*"]
    for svc in items:
        if svc is None:
            lines.append("  — unavailable")
            continue
        name     = svc.get("app_name", "unknown")
        mem      = svc.get("memory_working_set_bytes_avg")
        err      = svc.get("error_rate_percent")
        resp     = svc.get("avg_response_time_ms")
        reqs     = svc.get("requests_total")

        mem_icon = _icon(mem, _APP_MEM_WARN, _APP_MEM_CRIT)
        err_icon = _icon(err, _APP_ERR_WARN, _APP_ERR_CRIT)
        status   = _worst(mem_icon, err_icon)

        parts = [f"Mem {mem_icon} {_bytes_mb(mem)}"]
        if reqs is not None:
            parts.append(f"Req {_num(reqs)}")
        if err is not None:
            parts.append(f"Err {err_icon} {_pct(err)}")
        if resp is not None:
            parts.append(f"RT {_ms(resp)}")

        lines.append(f"  {status} **{name}**  ·  " + "  ·  ".join(parts))
    return lines


def _mysql_lines(items: list) -> list[str]:
    lines = ["*MySQL*"]
    for srv in items:
        if srv is None:
            lines.append("  — unavailable")
            continue
        name = srv.get("server_name", "unknown")
        cpu  = srv.get("cpu_percent_avg")
        mem  = srv.get("memory_percent_avg")
        conn = srv.get("active_connections_avg")
        io   = srv.get("io_percent_avg")

        ci = _icon(cpu, _DB_CPU_WARN, _DB_CPU_CRIT)
        mi = _icon(mem, _DB_MEM_WARN, _DB_MEM_CRIT)
        ii = _icon(io,  _DB_IO_WARN)
        status = _worst(ci, mi, ii)

        lines.append(
            f"  {status} **{name}**  ·  "
            f"CPU {ci} {_pct(cpu)}  ·  "
            f"Mem {mi} {_pct(mem)}  ·  "
            f"Conn {_conn(conn)}  ·  "
            f"IO {ii} {_pct(io)}"
        )
    return lines


def _postgres_lines(items: list) -> list[str]:
    lines = ["*PostgreSQL*"]
    for srv in items:
        if srv is None:
            lines.append("  — unavailable")
            continue
        name = srv.get("server_name", "unknown")
        cpu  = srv.get("cpu_percent_avg")
        mem  = srv.get("memory_percent_avg")
        conn = srv.get("active_connections_avg")
        io   = srv.get("iops_avg")

        ci = _icon(cpu, _DB_CPU_WARN, _DB_CPU_CRIT)
        mi = _icon(mem, _DB_MEM_WARN, _DB_MEM_CRIT)
        ii = _icon(io,  _DB_IO_WARN)
        status = _worst(ci, mi, ii)

        lines.append(
            f"  {status} **{name}**  ·  "
            f"CPU {ci} {_pct(cpu)}  ·  "
            f"Mem {mi} {_pct(mem)}  ·  "
            f"Conn {_conn(conn)}  ·  "
            f"IO {ii} {_pct(io, 0)} iops"
        )
    return lines


def _redis_lines(items: list) -> list[str]:
    lines = ["*Redis*"]
    for cache in items:
        if cache is None:
            lines.append("  — unavailable")
            continue
        name    = cache.get("cache_name", "unknown")
        mem_pct = cache.get("used_memory_percent_avg")
        clients = cache.get("connected_clients_avg")
        load    = cache.get("server_load_avg")

        mi = _icon(mem_pct, _REDIS_MEM_WARN, _REDIS_MEM_CRIT)
        li = _icon(load, _REDIS_LOAD_WARN)
        status = _worst(mi, li)

        lines.append(
            f"  {status} **{name}**  ·  "
            f"Mem {mi} {_pct(mem_pct)}  ·  "
            f"Clients {_conn(clients)}  ·  "
            f"Load {li} {_pct(load)}"
        )
    return lines


def _service_bus_lines(items: list) -> list[str]:
    lines = ["*Service Bus*"]
    for ns in items:
        if ns is None:
            lines.append("  — unavailable")
            continue
        name     = ns.get("namespace_name", "unknown")
        active   = ns.get("active_messages_avg")
        dead     = ns.get("deadlettered_messages_avg")
        incoming = ns.get("incoming_messages_total")
        outgoing = ns.get("outgoing_messages_total")

        di = _icon(dead,   _SB_DEAD_WARN,   _SB_DEAD_CRIT)
        ai = _icon(active, _SB_ACTIVE_WARN)
        status = _worst(di, ai)

        throughput = f"In {_num(incoming)}  Out {_num(outgoing)}" if incoming is not None else ""

        lines.append(
            f"  {status} **{name}**  ·  "
            f"Active {ai} {_num(active)}  ·  "
            f"Dead {di} {_num(dead)}"
            + (f"  ·  {throughput}" if throughput else "")
        )
    return lines


def format_project_section(project_name: str, data: dict) -> list[str]:
    rg    = data.get("resource_group", "")
    lines = [f"**🏢  {project_name}**  `{rg}`"]

    if data.get("app_services"):
        lines.extend(_app_service_lines(data["app_services"]))

    if data.get("mysql"):
        lines.extend(_mysql_lines(data["mysql"]))

    if data.get("postgres"):
        lines.extend(_postgres_lines(data["postgres"]))

    if data.get("redis"):
        lines.extend(_redis_lines(data["redis"]))

    if data.get("service_bus"):
        lines.extend(_service_bus_lines(data["service_bus"]))

    return lines


# ---------------------------------------------------------------------------
# Top-level assembler
# ---------------------------------------------------------------------------

_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def format_daily_report(
    aks_data: dict,
    azure_data: dict,
    report_date: date | None = None,
) -> str:
    today = (report_date or date.today()).strftime("%Y-%m-%d")

    lines: list[str] = [
        f"📊 **Daily Production Health Report**",
        f"📅 {today}  ·  Last 24h",
        "",
        _DIVIDER,
        format_aks_section(aks_data),
    ]

    for project in azure_data.get("projects", []):
        lines.append(_DIVIDER)
        lines.extend(format_project_section(project["name"], project))

    lines.append(_DIVIDER)

    report = "\n".join(lines)

    if len(report) > 20_000:
        report = report[:19_900] + "\n\n_(report truncated)_"

    return report
