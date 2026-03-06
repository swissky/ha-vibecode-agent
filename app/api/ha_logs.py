"""Home Assistant System Logs API - uses HA WebSocket system_log/list and Supervisor API"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
import logging
import os
import aiohttp

router = APIRouter()
logger = logging.getLogger('ha_cursor_agent')


def _get_supervisor_token() -> str:
    return os.environ.get('SUPERVISOR_TOKEN', '')


async def _fetch_via_websocket(filter_zendure: bool = False) -> List[dict]:
    """
    Fetch system_log entries via HA WebSocket API (system_log/list).
    Returns list of log entry dicts with keys: name, level, message, timestamp, exception.
    """
    try:
        from app.services.ha_websocket import get_ws_client
        ws = await get_ws_client()
        result = await ws._send_message({
            'type': 'system_log/list'
        }, timeout=15.0)
        entries = result if isinstance(result, list) else []
        if filter_zendure:
            entries = [
                e for e in entries
                if 'zendure' in str(e.get('name', '')).lower()
                or 'zendure' in ' '.join(e.get('message', [])).lower()
            ]
        return entries
    except Exception as e:
        logger.warning(f"system_log WebSocket fetch failed: {e}")
        return []


async def _fetch_supervisor_host_logs(identifier: str = "homeassistant", num_entries: int = 500) -> Optional[str]:
    """
    Try Supervisor /host/logs/ API. Returns None if unavailable.
    """
    token = _get_supervisor_token()
    if not token:
        return None
    urls_to_try = [
        f"http://supervisor/host/logs/{identifier}/entries",
        f"http://supervisor/host/logs/all/entries",
    ]
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'text/plain',
        'Range': f'entries=:-{num_entries}:{num_entries}',
    }
    async with aiohttp.ClientSession() as session:
        for url in urls_to_try:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    logger.info(f"Supervisor host/logs response: {resp.status} for {url}")
                    if resp.status == 200:
                        return await resp.text()
            except Exception as e:
                logger.debug(f"Supervisor host/logs failed for {url}: {e}")
    return None


def _format_ws_entries_as_lines(entries: List[dict]) -> List[str]:
    """Convert WebSocket system_log entries to log-line strings"""
    lines = []
    for e in entries:
        ts = e.get('timestamp', '')
        level = e.get('level', 'unknown').upper()
        name = e.get('name', 'unknown')
        messages = e.get('message', [])
        if isinstance(messages, list):
            msg = ' | '.join(messages)
        else:
            msg = str(messages)
        count = e.get('count', 1)
        suffix = f" (x{count})" if count > 1 else ""
        lines.append(f"{ts} {level} {name} {msg}{suffix}")
    return lines


@router.get("/ha_system")
async def get_ha_system_logs(
    tail: int = Query(200, description="Number of lines to return", ge=1, le=5000),
    filter: Optional[str] = Query(None, description="Filter string (case-insensitive)"),
    level: Optional[str] = Query(None, description="Filter by level: ERROR, WARNING, INFO, DEBUG"),
    identifier: str = Query("homeassistant", description="Supervisor log identifier"),
    source: str = Query("auto", description="Source: auto, websocket, supervisor"),
):
    """
    Get Home Assistant system logs.

    **Sources (tried in order):**
    1. Supervisor /host/logs/ API (native systemd-journal logs)
    2. HA WebSocket system_log/list (structured log entries - up to max_entries in config)

    **Examples:**
    - `/api/ha_logs/ha_system?tail=100&filter=zendure` - Zendure log lines
    - `/api/ha_logs/ha_system?level=ERROR` - Only errors
    - `/api/ha_logs/ha_system?source=websocket` - Force WebSocket source
    """
    lines = []
    log_source = None

    # Try Supervisor API first (unless forced to websocket)
    if source in ("auto", "supervisor"):
        raw = await _fetch_supervisor_host_logs(identifier=identifier, num_entries=min(tail * 5, 3000))
        if raw is not None:
            all_lines = raw.splitlines()
            lines = all_lines
            log_source = f"supervisor_host_logs:{identifier}"

    # Fallback: WebSocket system_log/list
    if not lines and source in ("auto", "websocket"):
        entries = await _fetch_via_websocket(filter_zendure=False)
        lines = _format_ws_entries_as_lines(entries)
        log_source = "websocket_system_log"

    if not lines:
        raise HTTPException(
            status_code=503,
            detail=f"No log source available. Token: {bool(_get_supervisor_token())}. "
                   f"Try /api/ha_logs/ha_system?source=websocket or check addon hassio_role: manager."
        )

    if level:
        lines = [l for l in lines if level.upper() in l.upper()]
    if filter:
        lines = [l for l in lines if filter.lower() in l.lower()]

    lines = lines[-tail:]

    return {
        "success": True,
        "source": log_source,
        "total_lines": len(lines),
        "filter_applied": filter,
        "level_filter": level,
        "tail": tail,
        "lines": lines,
        "raw": "\n".join(lines),
    }


@router.get("/ha_system/zendure")
async def get_zendure_logs(
    tail: int = Query(100, description="Number of Zendure log lines to return", ge=1, le=2000),
    level: Optional[str] = Query(None, description="Filter by level: ERROR, WARNING, INFO, DEBUG"),
):
    """
    Get Zendure integration logs from HA system logs.

    Tries Supervisor logs first, then WebSocket system_log (pre-filtered for Zendure).

    **Examples:**
    - `/api/ha_logs/ha_system/zendure` - Last 100 Zendure lines
    - `/api/ha_logs/ha_system/zendure?level=ERROR` - Only Zendure errors
    """
    lines = []
    log_source = None

    # Try Supervisor host logs
    raw = await _fetch_supervisor_host_logs(identifier="homeassistant", num_entries=3000)
    if raw is not None:
        all_lines = raw.splitlines()
        lines = [l for l in all_lines if 'zendure' in l.lower()]
        log_source = "supervisor_host_logs"

    # Fallback: WebSocket
    if not lines:
        entries = await _fetch_via_websocket(filter_zendure=True)
        lines = _format_ws_entries_as_lines(entries)
        log_source = "websocket_system_log"

    if level:
        lines = [l for l in lines if level.upper() in l.upper()]

    lines = lines[-tail:]

    errors = [l for l in lines if 'ERROR' in l]
    warnings = [l for l in lines if 'WARNING' in l]
    p1_events = [l for l in lines if 'p1' in l.lower() or 'powerchanged' in l.lower()]

    return {
        "success": True,
        "source": log_source,
        "total_zendure_lines": len(lines),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "p1_event_count": len(p1_events),
        "recent_errors": errors[-10:],
        "recent_warnings": warnings[-10:],
        "recent_p1_events": p1_events[-5:],
        "all_lines": lines,
    }
