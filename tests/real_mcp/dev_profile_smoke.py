"""Opt-in local development-profile MCP smoke. No review request or writes."""
import asyncio
import os
from pathlib import Path
import sys
from datetime import timedelta

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


async def run():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
           "SORA_NEXT_OWNER_REVIEW_PILOT": "1",
           "SORA_NEXT_OWNER_PROFILE": "development",
           "SORA_NEXT_EDIT_PILOT": "0",
           "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "sora_commander_next.stdio_main"], cwd=str(ROOT), env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer,
                                 read_timeout_seconds=timedelta(seconds=8)) as session:
            await session.initialize()
            ping = await session.call_tool("ping", {})
            listing = await session.call_tool("list_folder", {"path": str(ROOT)})
            file = await session.call_tool("read_text_file", {
                "path": str(ROOT / "README.md")})
            names = {item.name for item in (await session.list_tools()).tools}
            assert not ping.isError and ping.structuredContent["status"] == "OK"
            assert not listing.isError and any(x["name"] == "src" for x in
                                               listing.structuredContent["entries"])
            assert not file.isError and file.structuredContent["bytes"] > 0
            assert "preview_text_edit" in names and "request_text_edit" not in names
            assert "replace_text" not in names and "approve_local" not in names
            print("DEV_PROFILE_MCP_PASS=1", flush=True)
            print("ROOT_LISTED=1 READ=1 WRITE_API=0", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
