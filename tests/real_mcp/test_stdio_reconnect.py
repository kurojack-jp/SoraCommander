"""Disposable real-MCP stdio reconnect: no replays or production transport changes."""
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"src"))

async def probe_once():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env={"PYTHONPATH":str(ROOT/"src"),"PYTHONUTF8":"1","PYTHONDONTWRITEBYTECODE":"1"}
    params=StdioServerParameters(command=sys.executable,
        args=["-m","sora_commander_next.stdio_main"],cwd=str(ROOT),env=env)
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=5)) as client:
            await client.initialize()
            names={x.name for x in (await client.list_tools()).tools}
            assert {"run_command","list_sessions","previous_sessions"} <= names, names
            ping=await client.call_tool("ping",{})
            assert ping.structuredContent["status"]=="OK"
            sessions=await client.call_tool("list_sessions",{})
            assert sessions.structuredContent["count"]==0
            previous=await client.call_tool("previous_sessions",{})
            assert previous.structuredContent["count"]==0
            denied=await client.call_tool("run_command",{
                "executable":sys.executable,"argv":["--version"],"cwd":str(ROOT)})
            assert denied.structuredContent["status"]=="DENY"
            stale=await client.call_tool("read_process_output",{"session_id":"a"*48})
            assert stale.structuredContent["error"]=="UnknownSession"
            return tuple(sorted(names))

class StdioReconnect(unittest.TestCase):
    def test_two_independent_stdios_have_no_replay(self):
        names1=asyncio.run(probe_once())
        names2=asyncio.run(probe_once())
        self.assertEqual(names1,names2)
        print("STDIO_RECONNECT_PASS=1 NO_REPLAY=1 TOOL_COUNT=",len(names2))

if __name__=="__main__":
    unittest.main()
