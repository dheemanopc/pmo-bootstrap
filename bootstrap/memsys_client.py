"""High-level memsys client. Thin wrapper around the MCP stdio transport.

Same external API as the previous HTTP version — seed/init/hooks don't change.
Construction now takes either an existing MCPStdioClient OR a server config
({command, env, cwd}) that gets spawned on enter.
"""

from __future__ import annotations

from typing import Any

from .mcp_client import MCPStdioClient, MCPError


class MemsysError(RuntimeError):
    pass


class MemsysClient:
    """High-level memsys API backed by an MCP stdio client.

    Two construction modes:

      # 1. Pass an already-started MCPStdioClient:
      with MCPStdioClient([...]) as mcp:
          client = MemsysClient(mcp=mcp)
          client.memory_write(...)

      # 2. Pass server_config; client manages the subprocess lifecycle:
      with MemsysClient(server_config={"command": ["mem-mcp", "serve"]}) as client:
          client.memory_write(...)
    """

    def __init__(
        self,
        mcp: MCPStdioClient | None = None,
        server_config: dict | None = None,
    ):
        if mcp is None and server_config is None:
            raise MemsysError("MemsysClient requires either mcp= or server_config=")
        self._mcp = mcp
        self._owns_mcp = mcp is None
        self._server_config = server_config

    # ---- lifecycle (when we own the subprocess) ---------------------------

    def __enter__(self) -> "MemsysClient":
        if self._mcp is None:
            assert self._server_config is not None
            self._mcp = MCPStdioClient(
                command=self._server_config["command"],
                env=self._server_config.get("env"),
                cwd=self._server_config.get("cwd"),
            )
            self._mcp.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._owns_mcp and self._mcp is not None:
            self._mcp.close()
            self._mcp = None

    # ---- low-level --------------------------------------------------------

    def _call(self, tool: str, args: dict) -> Any:
        if self._mcp is None:
            raise MemsysError("MemsysClient not started (use 'with' or pass an open mcp client)")
        try:
            return self._mcp.call_tool(tool, args)
        except MCPError as e:
            raise MemsysError(str(e)) from None

    # ---- high-level wrappers ---------------------------------------------

    @staticmethod
    def _unwrap_memory(result: Any) -> dict:
        """Normalize a memory-returning response.

        Observed shapes for memory_get: {"memory": {...}, "history": [...]}.
        memory_write / memory_supersede may use the same wrapper OR return the
        memory dict directly. Unwrap defensively.
        """
        if not isinstance(result, dict):
            return {"raw": result}
        if "memory" in result and isinstance(result["memory"], dict):
            return result["memory"]
        # Direct shape — common return for write operations.
        return result

    def memory_write(
        self,
        *,
        team_id: str,
        type: str,
        content: str,
        tags: list[str] | None = None,
        slug_clue: str | None = None,
        indexable: bool = True,
        parent_id: str | None = None,
        references: list[dict] | None = None,
    ) -> dict:
        args: dict[str, Any] = {
            "team_id": team_id,
            "type": type,
            "content": content,
            "tags": tags or [],
            "indexable": indexable,
        }
        if slug_clue:
            args["slug_clue"] = slug_clue
        if parent_id:
            args["parent_id"] = parent_id
        if references:
            args["references"] = references
        return self._unwrap_memory(self._call("memory_write", args))

    def memory_supersede(
        self,
        *,
        id: str,
        content: str,
        team_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        args: dict[str, Any] = {"id": id, "content": content}
        if team_id:
            args["team_id"] = team_id
        if tags is not None:
            args["tags"] = tags
        return self._unwrap_memory(self._call("memory_supersede", args))

    def memory_get(
        self,
        *,
        id: str | None = None,
        team_id: str | None = None,
        resource_type: str | None = None,
        slug: str | None = None,
    ) -> dict | None:
        args: dict[str, Any] = {}
        if id:
            args["id"] = id
        if team_id:
            args["team_id"] = team_id
        if resource_type:
            args["resource_type"] = resource_type
        if slug:
            args["slug"] = slug
        result = self._call("memory_get", args)
        if not result:
            return None
        unwrapped = self._unwrap_memory(result)
        return unwrapped if unwrapped else None

    def slug_lookup(self, *, team_id: str, resource_type: str, slug: str) -> dict | None:
        result = self._call(
            "memsys_slug_lookup",
            {"team_id": team_id, "resource_type": resource_type, "slug": slug},
        )
        if not result:
            return None
        if isinstance(result, dict) and result.get("memory_id") is None:
            return None
        return result if isinstance(result, dict) else None

    def list_my_teams(self) -> list[dict]:
        result = self._call("memsys_list_my_teams", {})
        if isinstance(result, dict):
            return result.get("teams", []) or result.get("results", []) or []
        return result if isinstance(result, list) else []

    def team_create(self, name: str, description: str = "") -> dict:
        result = self._call("memsys_create_team", {"name": name, "description": description})
        return result if isinstance(result, dict) else {"raw": result}

    def ping(self) -> bool:
        try:
            self.list_my_teams()
            return True
        except MemsysError:
            return False
