"""
MCP Loader for Google Antigravity.
Discovers and loads MCP server configurations from global and project locations,
converting them into Antigravity SDK McpServer instances and safety policies.
"""

import json
import os
from pathlib import Path
from typing import List, Tuple, Any
from google.antigravity import types
from google.antigravity.hooks import policy


def load_mcp_servers_from_config(config_path: Path) -> List[Any]:
    """Parse an mcp_config.json file and return initialized McpServer objects."""
    if not config_path.exists():
        return []

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[MCP Loader] 无法读取配置文件 {config_path}: {e}")
        return []

    servers_dict = data.get("mcpServers", {})
    loaded = []

    for name, s_cfg in servers_dict.items():
        try:
            # 1. Streamable HTTP Server
            url = s_cfg.get("serverUrl") or s_cfg.get("url")
            if url:
                headers = s_cfg.get("headers")
                http_server = types.McpStreamableHttpServer(
                    name=name,
                    url=url,
                    headers=headers,
                )
                loaded.append(http_server)
                print(f"[MCP Loader] 已载入 HTTP MCP 服务: {name} ({url})")
                continue

            # 2. Stdio Server
            command = s_cfg.get("command")
            if command:
                args = s_cfg.get("args", [])
                env = s_cfg.get("env")
                stdio_server = types.McpStdioServer(
                    name=name,
                    command=command,
                    args=args,
                    env=env,
                )
                loaded.append(stdio_server)
                print(f"[MCP Loader] 已载入 Stdio MCP 服务: {name} ({command} {' '.join(args)})")
                continue

            print(f"[MCP Loader] 跳过未知配置的 MCP 服务: {name}")
        except Exception as e:
            print(f"[MCP Loader] 初始化服务 {name} 失败: {e}")

    return loaded


def discover_all_mcp_servers(workspace_root: str | None = None) -> Tuple[List[Any], List[Any]]:
    """
    Auto-discovers MCP servers from standard locations:
    1. Global: ~/.gemini/config/mcp_config.json
    2. Project: <workspace_root>/.agents/mcp_config.json
    Returns:
        (mcp_servers, policies)
    """
    home = Path.home()
    global_config = home / ".gemini" / "config" / "mcp_config.json"
    
    servers = []
    
    # 1. Load global
    if global_config.exists():
        servers.extend(load_mcp_servers_from_config(global_config))
        
    # 2. Load workspace-level if exists
    if workspace_root:
        ws_path = Path(workspace_root).resolve()
        project_config = ws_path / ".agents" / "mcp_config.json"
        if project_config.exists():
            servers.extend(load_mcp_servers_from_config(project_config))

    # 生成安全策略：显式允许已加载的所有 MCP 服务及其工具
    policies = []
    for s in servers:
        policies.append(policy.allow(s))

    return servers, policies


if __name__ == "__main__":
    servers, policies = discover_all_mcp_servers()
    print(f"\n共发现并载入 {len(servers)} 个 MCP 服务:")
    for s in servers:
        print(f" - [{s.type}] {s.name}")
