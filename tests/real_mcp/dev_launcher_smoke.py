"""Real MCP smoke for the staged cmd launcher, without the production tunnel."""
import asyncio
from datetime import timedelta
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]

async def main():
    params = StdioServerParameters(
        command="cmd.exe", args=["/d", "/c", str(ROOT / "tools" / "next-development.cmd")],
        cwd=str(ROOT))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer,
                                 read_timeout_seconds=timedelta(seconds=10)) as session:
            await session.initialize()
            listing = await session.call_tool("list_folder", {"path": str(ROOT), "limit": 100})
            text = await session.call_tool("read_text_file", {"path": str(ROOT / "README.md")})
            names = {t.name for t in (await session.list_tools()).tools}
            assert not listing.isError and any(e["name"] == "src" for e in listing.structuredContent["entries"])
            assert not text.isError and text.structuredContent["bytes"] > 0
            assert "request_text_edit" not in names
            assert "preview_text_edit" in names
            print("DEV_LAUNCHER_MCP_PASS=1", flush=True)
            print("ROOT_READ=1 REMOTE_WRITE=0", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
