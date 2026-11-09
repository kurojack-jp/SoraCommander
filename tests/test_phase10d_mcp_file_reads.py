"""Real MCP SDK dispatch/IO with mock path admission; not live Host acceptance."""
import asyncio,pathlib,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import build_server
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
class MCPReadTests(unittest.TestCase):
 def setUp(self):
  self.root=pathlib.Path(self.enterContext(inherited_test_temp())).resolve()
  self.enterContext(mock_read_boundary(self.root, ("readme.txt",)))
  self.p=self.root/"readme.txt";self.p.write_text("hello from MCP",encoding="utf-8")
 def test_registered_tools(self):
  h=Host(policy=Policy(roots=(str(self.root),)))
  try:
   server=build_server(h)
   self.assertEqual({tool.name for tool in server._tool_manager.list_tools()},
                    {"ping","status","run_command","run_command_wait","await_command_result",
                     "stop_process","stop_process_wait","read_process_output",
                     "await_process_output","list_folder","list_folder_page","read_text_file","read_text_page","find_files","search_code","path_info","read_text_lines","read_many_text_files","compare_text_files","read_text_chunk","sha256_file",
                     "review_status","preview_text_edit","preview_source_create","preview_source_patch","tool_catalog","list_sessions","previous_sessions","recovery_overview","request_status","previous_output",
                     "create_directory","create_file","move_file","request_source_patch_dev","list_processes","get_config","send_input"})
  finally:h.jobs.close()
 def test_reads_through_real_sdk(self):
  h=Host(policy=Policy(roots=(str(self.root),)))
  try:
   s=build_server(h)
   out=asyncio.run(s.call_tool("read_text_file",{"path":str(self.p)}))
   self.assertIn("hello from MCP",str(out))
   listing=asyncio.run(s.call_tool("list_folder",{"path":str(self.root)}))
   self.assertIn("readme.txt",str(listing))
  finally:h.jobs.close()
 def test_empty_roots_rejected(self):
  h=Host()
  try:
   s=build_server(h)
   from mcp.server.fastmcp.exceptions import ToolError
   with self.assertRaises(ToolError):
    asyncio.run(s.call_tool("read_text_file",{"path":str(self.p)}))
  finally:h.jobs.close()
class NavigationWireReviewTests(unittest.TestCase):
 def test_read_bounds_and_nul_paths_rejected_before_sdk(self):
  from sora_commander_next.mcp_adapter import _validate_arguments
  for name, args in (
   ("read_text_page", {"path":"x", "max_bytes":0}),
   ("read_text_page", {"path":"x", "max_bytes":65537}),
   ("list_folder_page", {"path":"x", "limit":201}),
   ("list_folder_page", {"path":"x", "limit":0}),
   ("read_many_text_files", {"paths":["x\0secret"]}),
   ("compare_text_files", {"before_path":"x\0secret", "after_path":"y"}),
   ("path_info", {"path":"x\0secret"}),
  ):
   with self.subTest(name=name,args=args),self.assertRaises(ValueError):
    _validate_arguments(name,args)

 def test_all_eight_tools_sanitize_errors_before_sdk_logging(self):
  import io,logging
  from unittest.mock import patch
  from mcp.server.fastmcp.exceptions import ToolError
  h=Host(policy=Policy(roots=(str(ROOT / "tests"),)))
  capture=io.StringIO();handler=logging.StreamHandler(capture)
  logger=logging.getLogger();logger.addHandler(handler)
  try:
   sdk=build_server(h)
   cases=(
    ("read_text_page",{"path":"x"}), ("list_folder_page",{"path":"x"}),
    ("find_files",{"path":"x","name":"x"}),
    ("search_code",{"path":"x","text":"x"}),
    ("path_info",{"path":"x"}), ("read_text_lines",{"path":"x"}),
    ("read_many_text_files",{"paths":["x"]}),
    ("compare_text_files",{"before_path":"x","after_path":"y"}),
   )
   for name,args in cases:
    with self.subTest(name=name),patch("sora_commander_next.mcp_adapter."+name+"_readonly",side_effect=OSError("PRIVATE_PATH_SENTINEL")):
     with self.assertRaises(ToolError) as error:
      asyncio.run(sdk.call_tool(name,args))
     self.assertNotIn("PRIVATE_PATH_SENTINEL",str(error.exception))
   self.assertNotIn("PRIVATE_PATH_SENTINEL",capture.getvalue())
  finally:
   logger.removeHandler(handler);handler.close();h.jobs.close()

if __name__=="__main__":unittest.main()
