"""Real MCP SDK stdio registration smoke; never requests an actual edit."""
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[2]
async def check(flag):
    env={**os.environ, "PYTHONPATH":str(ROOT/"src"),
         "SORA_NEXT_OWNER_REVIEW_PILOT":"1",
         "SORA_NEXT_OWNER_PROFILE":"development",
         "SORA_NEXT_EDIT_PILOT":flag}
    params=StdioServerParameters(command=sys.executable,
             args=["-m","sora_commander_next.stdio_main"],cwd=str(ROOT),env=env)
    async with stdio_client(params) as (rd,wr):
        async with ClientSession(rd,wr,read_timeout_seconds=timedelta(seconds=10)) as session:
            await session.initialize()
            names={t.name for t in (await session.list_tools()).tools}
            assert ("request_text_edit" in names)==(flag=="1")
            assert ("edit_review_status" in names)==(flag=="1")
            assert "preview_text_edit" in names
            if flag=="1":
                from hashlib import sha256
                fixture=ROOT/"tests"/"real_mcp"/"owner-edit-pilot-live.txt"
                digest=sha256(fixture.read_bytes()).hexdigest()
                outcome=await session.call_tool("request_text_edit",{
                    "path":str(ROOT/"README.md"),"content":"wrong target",
                    "expected_sha256":digest})
                assert not outcome.isError
                assert outcome.structuredContent["error"]=="EDIT_REJECTED"
            print("EDIT_STDIO_FLAG="+flag+" PASS",flush=True)
async def main():
    await check("0")
    await check("1")
    print("EDIT_STDIO_SMOKE_PASS=1",flush=True)
asyncio.run(main())
