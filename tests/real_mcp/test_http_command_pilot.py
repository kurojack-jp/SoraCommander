"""Phase10K: real SDK HTTP in-process command roundtrip, local test only."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Pattern,Policy
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.http_local import _BearerGate
from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next.windows_owner_controller import WindowsOwnerController

class HTTPCommandPilot(unittest.IsolatedAsyncioTestCase):
 async def test_auth_and_allowed_version_command(self):
  import httpx
  from mcp import ClientSession
  from mcp.client.streamable_http import streamablehttp_client
  from mcp.server.transport_security import TransportSecuritySettings
  import uvicorn
  import socket
  import anyio
  # This fixture is local test-only, permits exactly Python --version under tests.
  path=str(ROOT/"tests")
  policy=Policy(roots=(path,),auto=(Pattern(sys.executable,("--version",)),))
  guard=OwnerSessionGuard()
  guard.verify()
  host=Host(policy=policy, owner=guard.sid)
  controller=WindowsOwnerController(host)
  controller._verify()
  token=secrets.token_hex(32)
  server=build_server(host)
  server.settings.transport_security=TransportSecuritySettings(
   enable_dns_rebinding_protection=True,allowed_hosts=["127.0.0.1:*"],
   allowed_origins=["http://127.0.0.1:*"])
  server.settings.json_response=True
  app=server.streamable_http_app()
  wrapped=_BearerGate(app,token,health=host.ping)
  listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
  listener.bind(("127.0.0.1",0));listener.listen(16)
  port=listener.getsockname()[1]
  config=uvicorn.Config(wrapped,host="127.0.0.1",port=0,
                        access_log=False,log_level="critical",
                        lifespan="on",proxy_headers=False,ws="none")
  service=uvicorn.Server(config)
  task=asyncio.create_task(service.serve(sockets=[listener]))
  try:
   for _ in range(100):
    if service.started:break
    await asyncio.sleep(.025)
   self.assertTrue(service.started)
   url=f"http://127.0.0.1:{port}/mcp"
   async with httpx.AsyncClient(timeout=3) as http:
    denied=await http.get(f"http://127.0.0.1:{port}/__sora_health")
    self.assertEqual(denied.status_code,401)
   async with streamablehttp_client(url,headers={"Authorization":"Bearer "+token}) as streams:
    async with ClientSession(streams[0],streams[1]) as client:
     await client.initialize()
     result=await client.call_tool("run_command",
      {"executable":sys.executable,"argv":["--version"],"cwd":path})
     self.assertFalse(result.isError,str(result))
     data=result.structuredContent
     self.assertEqual(data["status"],"COMPLETED")
     self.assertEqual(data["exit_code"],0)
     self.assertIn("Python",data["stdout"]+data["stderr"])
     self.assertEqual(data["decision"],"AUTO")
     pending=await client.call_tool("run_command",
      {"executable":sys.executable,"argv":["-c","print(12345)"],"cwd":path})
     self.assertFalse(pending.isError,str(pending))
     self.assertEqual(pending.structuredContent["status"],"REVIEW")
     self.assertTrue(pending.structuredContent["pending_id"])
     self.assertEqual(len(controller.pending_local()),1)
  finally:
   service.should_exit=True
   await asyncio.wait_for(task,timeout=5)
   listener.close()
   host.jobs.close()

if __name__=="__main__":unittest.main()
