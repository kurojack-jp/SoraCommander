"""Disposable SDK stdio crash/reconnect; never controls the live tunnel."""
import asyncio
from datetime import timedelta
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"src"))

async def crash_reconnect():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client import stdio
    import anyio
    from unittest.mock import patch
    processes=[]
    original=stdio._create_platform_compatible_process
    async def track(*args,**kwargs):
        proc=await original(*args,**kwargs)
        processes.append(proc)
        return proc
    params=StdioServerParameters(command=sys.executable,
        args=["-m","sora_commander_next.stdio_main"],cwd=str(ROOT),
        env={"PYTHONPATH":str(ROOT/"src"),"PYTHONUTF8":"1"})
    with patch.object(stdio,"_create_platform_compatible_process",track):
        try:
            with anyio.fail_after(18):
                try:
                    async with stdio.stdio_client(params) as (rd,wr):
                        async with ClientSession(rd,wr,read_timeout_seconds=timedelta(seconds=3)) as client:
                            await client.initialize()
                            assert (await client.call_tool("ping",{})).structuredContent["status"]=="OK"
                            assert len(processes)==1
                            processes[0].kill()
                            await processes[0].wait()
                            try:
                                await client.call_tool("ping",{})
                                raise AssertionError("dead host accepted a call")
                            except (AssertionError,):
                                raise
                            except Exception:
                                pass
                except (Exception,BaseExceptionGroup) as exc:
                    # The transport is expected to fail when its disposable host dies.
                    if isinstance(exc,AssertionError):
                        raise
                async with stdio.stdio_client(params) as (rd,wr):
                    async with ClientSession(rd,wr,read_timeout_seconds=timedelta(seconds=3)) as client:
                        await client.initialize()
                        assert len(processes)==2
                        assert processes[0].pid!=processes[1].pid
                        assert (await client.call_tool("ping",{})).structuredContent["status"]=="OK"
                        for tool in ("list_sessions","previous_sessions"):
                            result=await client.call_tool(tool,{})
                            assert result.structuredContent["count"]==0
                        denied=await client.call_tool("run_command",{
                            "executable":sys.executable,"argv":["--version"],"cwd":str(ROOT)})
                        assert denied.structuredContent["status"]=="DENY"
                        print("STDIO_CRASH_RECONNECT_PASS=1 NEW_PID=1 NO_REPLAY=1",flush=True)
        finally:
            for proc in processes:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

class CrashReconnect(unittest.TestCase):
    def test_disposable_host_forced_exit_then_fresh_connection(self):
        asyncio.run(crash_reconnect())

if __name__=="__main__":
    unittest.main()
