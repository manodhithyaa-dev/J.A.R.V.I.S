#!/usr/bin/env python3
"""
DEEPSEARCH MCP Server (MCP Python SDK v2)

Exposes the existing DEEPSEARCH FastAPI backend as MCP tools.
The FastAPI service must be running separately.

Run:
    python server.py

Environment:
    DEEPSEARCH_API_URL=http://127.0.0.1:8001

Install:
    pip install "mcp[cli]" httpx python-dotenv
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("deepsearch-mcp")

API_BASE_URL = os.getenv(
    "DEEPSEARCH_API_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

# Keep this comfortably below typical MCP client timeouts. The long-running
# research job itself runs asynchronously in FastAPI.
HTTP_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=float(os.getenv("DEEPSEARCH_API_READ_TIMEOUT", "30")),
    write=30.0,
    pool=10.0,
)

mcp = MCPServer(
    name="DEEPSEARCH",
    instructions=(
        "Tools for starting and managing DEEPSEARCH web research jobs. "
        "Research runs asynchronously in the DEEPSEARCH FastAPI backend. "
        "Start a job with deepsearch, poll it with get_search_status, and "
        "retrieve its completed Markdown using get_search_report."
    ),
)


async def api_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the existing FastAPI backend and normalize errors."""
    url = f"{API_BASE_URL}{path}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.request(
                method,
                url,
                json=payload,
                params=params,
            )
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"Cannot connect to DEEPSEARCH API at {API_BASE_URL}. "
            "Start the FastAPI backend first."
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"DEEPSEARCH API request timed out: {path}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"DEEPSEARCH API request failed: {exc}") from exc

    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except (ValueError, AttributeError):
            detail = response.text
        raise RuntimeError(f"DEEPSEARCH API returned HTTP {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("DEEPSEARCH API returned invalid JSON.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("DEEPSEARCH API returned an unexpected response.")
    return data


def pretty(data: Any) -> str:
    """Return readable JSON for MCP clients."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
async def deepsearch(
    topic: str,
    max_depth: int | None = None,
    max_urls: int | None = None,
    max_pages: int | None = None,
    max_queries: int | None = None,
    max_duration: int | None = None,
) -> str:
    """
    Start a DEEPSEARCH research job.

    Returns a job_id immediately. Use get_search_status to monitor progress
    and get_search_report to retrieve the Markdown report after completion.

    Args:
        topic: Research topic (2-300 characters).
        max_depth: Recursive crawl depth (0-6).
        max_urls: Maximum discovered URLs (1-2000).
        max_pages: Maximum pages to fetch (1-500).
        max_queries: Maximum search queries (1-200).
        max_duration: Maximum job duration in seconds (30-86400).
    """
    if not topic or len(topic.strip()) < 2:
        return "Error: topic must contain at least 2 characters."

    payload = {
        "topic": topic.strip(),
        "max_depth": max_depth,
        "max_urls": max_urls,
        "max_pages": max_pages,
        "max_queries": max_queries,
        "max_duration": max_duration,
    }
    payload = {key: value for key, value in payload.items() if value is not None}

    result = await api_request("POST", "/api/research", payload=payload)
    return (
        "Research job started.\n"
        f"Job ID: {result.get('job_id')}\n"
        f"Status: {result.get('status', 'queued')}\n"
        "Next: call get_search_status(job_id), then get_search_report(job_id) "
        "when the status is completed."
    )


@mcp.tool()
async def get_search_status(job_id: str) -> str:
    """Get the status, phase, source counts, query count, and statistics of a job."""
    if not job_id.strip():
        return "Error: job_id is required."

    result = await api_request("GET", f"/api/research/{job_id.strip()}")
    # The full report is available through get_search_report, not status polling.
    result.pop("markdown", None)
    return pretty(result)


@mcp.tool()
async def get_search_report(job_id: str) -> str:
    """
    Retrieve the completed Markdown report for a research job.

    If the job is still running, returns its current status instead.
    """
    if not job_id.strip():
        return "Error: job_id is required."

    result = await api_request("GET", f"/api/research/{job_id.strip()}")
    status = result.get("status")

    if status != "completed":
        return (
            f"Report is not ready. Current status: {status}\n"
            f"Current phase: {result.get('phase')}\n"
            f"Job ID: {job_id}"
        )

    markdown = result.get("markdown") or ""
    if not markdown.strip():
        return "The job is completed, but the backend returned an empty Markdown report."

    return markdown


@mcp.tool()
async def list_search_jobs() -> str:
    """List the latest DEEPSEARCH jobs, including their status and phase."""
    result = await api_request("GET", "/api/research")
    return pretty(result)


@mcp.tool()
async def get_search_sources(
    job_id: str,
    status: str | None = None,
) -> str:
    """
    List source metadata collected by a job.

    Optional status filter examples: kept, rejected, failed, queued.
    """
    if not job_id.strip():
        return "Error: job_id is required."

    params = {"status": status} if status else None
    result = await api_request(
        "GET",
        f"/api/research/{job_id.strip()}/sources",
        params=params,
    )
    return pretty(result)


@mcp.tool()
async def cancel_search(job_id: str) -> str:
    """Request cancellation of a currently running research job."""
    if not job_id.strip():
        return "Error: job_id is required."

    result = await api_request("POST", f"/api/research/{job_id.strip()}/cancel")
    return pretty(result)


@mcp.tool()
async def resume_search(job_id: str) -> str:
    """Resume a cancelled, interrupted, or failed research job."""
    if not job_id.strip():
        return "Error: job_id is required."

    result = await api_request("POST", f"/api/research/{job_id.strip()}/resume")
    return pretty(result)


@mcp.tool()
async def deepsearch_health() -> str:
    """Check DEEPSEARCH API, Ollama model availability, and search providers."""
    result = await api_request("GET", "/api/health")
    return pretty(result)


if __name__ == "__main__":
    # FastMCP uses stdio by default. Do not print ordinary logs to stdout:
    # stdout is reserved for the MCP protocol.
    # mcp.run()
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8100,
    )
