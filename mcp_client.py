"""VAANI MCP client: plug in outside tool servers (Windows-MCP, Home Assistant, GitHub, Notion, filesystem...).

Config file mcp_servers.json (in VAANI's data folder, or the path in VAANI_MCP_CONFIG):
{
  "servers": {
    "windows": {
      "command": "uvx", "args": ["windows-mcp"],
      "allow":   ["Snapshot", "Click", "Type", "Scroll", "Shortcut", "App"],    // ONLY these tools may run ("*" = all)
      "confirm": ["Shell", "PowerShell"]                                         // these ask "say confirm" first
    }
  }
}
Nothing is exposed unless it is in "allow". No config file = no MCP = nothing changes.
Needs: pip install mcp   (and whatever the server itself needs, e.g. uv for uvx)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import paths

log = logging.getLogger("vaani.mcp")
_state = {"loop": None, "thread": None, "servers": {}, "config": None, "ready": False, "error": ""}
_lock = threading.Lock()
CALL_TIMEOUT = 45


def _config_path():
    env = os.getenv("VAANI_MCP_CONFIG", "").strip()
    return Path(env) if env else (paths.data_dir() / "mcp_servers.json")


def load_config() -> dict:
    p = _config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        servers = data.get("servers", {})
        return {k: v for k, v in servers.items() if isinstance(v, dict) and v.get("command")}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("bad MCP config %s: %s", p, exc)
        return {}


def configured() -> bool:
    return bool(load_config())


# ----------------------------------------------------------------- background loop
def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_loop():
    with _lock:
        if _state["loop"] is None:
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=_run_loop, args=(loop,), daemon=True, name="VAANI-MCP")
            t.start()
            _state.update(loop=loop, thread=t)
    return _state["loop"]


async def _connect_one(name: str, cfg: dict):
    from contextlib import AsyncExitStack
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    stack = AsyncExitStack()
    params = StdioServerParameters(command=cfg["command"], args=[str(a) for a in cfg.get("args", [])],
                                   env={**os.environ, **{str(k): str(v) for k, v in (cfg.get("env") or {}).items()}})
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    listing = await session.list_tools()
    allow = cfg.get("allow", [])
    tools = {}
    for t in listing.tools:
        if allow == "*" or (isinstance(allow, list) and t.name in allow):
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or {}
            if not isinstance(schema, dict) and hasattr(schema, "model_dump"):
                schema = schema.model_dump()
            tools[t.name] = {"description": (t.description or "")[:160], "args": list((schema.get("properties") or {}).keys())[:8]}
    _state["servers"][name] = {"session": session, "stack": stack, "tools": tools, "confirm": set(cfg.get("confirm", []))}
    log.info("MCP server %s connected with %d allowed tools", name, len(tools))


def start(timeout: float = 25.0) -> bool:
    """Connect every configured server (call once, at startup, in a background thread). Never raises."""
    cfg = load_config()
    if not cfg:
        return False
    try:
        import mcp  # noqa: F401
    except ImportError:
        _state["error"] = "MCP support needs: pip install mcp"
        log.warning(_state["error"])
        return False
    loop = _ensure_loop()
    for name, c in cfg.items():
        try:
            asyncio.run_coroutine_threadsafe(_connect_one(name, c), loop).result(timeout)
        except Exception as exc:
            log.warning("MCP server %s failed to start: %s: %s", name, type(exc).__name__, str(exc)[:160])
            _state["error"] = f"{name}: {exc}"
    _state["ready"] = bool(_state["servers"])
    return _state["ready"]


def start_background() -> None:
    if configured():
        threading.Thread(target=start, daemon=True, name="VAANI-MCP-Start").start()


# ------------------------------------------------------------------ catalog / policy
def ready() -> bool:
    return bool(_state["servers"])


def is_allowed(server: str, tool: str) -> bool:
    s = _state["servers"].get(server)
    return bool(s and tool in s["tools"])


def needs_confirm(server: str, tool: str) -> bool:
    s = _state["servers"].get(server)
    return bool(s and tool in s["confirm"])


def prompt_section(max_chars: int = 1800) -> str:
    """Tool list for the planner prompt (only allowed tools). Empty when MCP isn't set up."""
    if not ready():
        return ""
    lines = []
    for sname, s in _state["servers"].items():
        for tname, t in s["tools"].items():
            lines.append(f'- mcp server="{sname}" tool="{tname}" args{{{",".join(t["args"])}}}: {t["description"]}')
    if not lines:
        return ""
    text = "\nEXTRA TOOLS (use step {\"do\":\"mcp\",\"server\":...,\"tool\":...,\"args\":{...}} ONLY when no built-in action fits):\n" + "\n".join(lines)
    return text[:max_chars]


def call(server: str, tool: str, args: dict | None = None):
    """-> (ok, spoken text). Refuses anything not in the allow list."""
    if not is_allowed(server, tool):
        return False, f"I'm not allowed to use {tool} on {server}."
    s = _state["servers"][server]
    loop = _state["loop"]
    if loop is None:
        return False, "The tool server isn't running."
    try:
        res = asyncio.run_coroutine_threadsafe(s["session"].call_tool(tool, args or {}), loop).result(CALL_TIMEOUT)
    except Exception as exc:
        log.warning("MCP call %s.%s failed: %s", server, tool, exc)
        return False, f"{tool} didn't answer ({type(exc).__name__})."
    text = " ".join(getattr(c, "text", "") for c in (res.content or []) if getattr(c, "text", "")).strip()
    if getattr(res, "isError", False):
        return False, (text or f"{tool} reported an error.")[:200]
    return True, (text[:300] or "Done.")


def status() -> str:
    if not configured():
        return "No tool servers are configured."
    if not ready():
        return _state["error"] or "Tool servers are still starting."
    return "Connected: " + ", ".join(f"{n} ({len(s['tools'])} tools)" for n, s in _state["servers"].items())


def stop() -> None:
    loop = _state["loop"]
    if not loop:
        return
    for s in list(_state["servers"].values()):
        try:
            asyncio.run_coroutine_threadsafe(s["stack"].aclose(), loop).result(5)
        except Exception:
            pass
    _state["servers"].clear()
    time.sleep(0.05)