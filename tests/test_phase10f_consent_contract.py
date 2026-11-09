"""Phase10F UI contract: repeat option cannot silently grant unsafe authority."""
import pathlib,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.owner_approval_dialog import parse_request
from sora_commander_next.mcp_adapter import _validate_arguments
class ConsentContractTests(unittest.TestCase):
 def test_unknown_permission_would_be_denied_by_broker(self):
  source=(ROOT/"src"/"sora_commander_next"/"owner_approval_broker.py").read_text(encoding="utf-8")
  self.assertIn('response["decision"] not in ("APPROVE","DENY","EXPIRED")',source)
  self.assertIn('if response["decision"]=="EXPIRED":',source)
  self.assertIn('expired=controller.reject_local(pending_id)',source)
  self.assertIn('"status":"TIMED_OUT"',source)
  self.assertIn("'FIRST_GUI', 'EXPIRED', 'EXPIRED', 'NOT_STARTED'",source)
 def test_repeat_option_disabled_until_session_invalidation(self):
  source=(ROOT/"src"/"sora_commander_next"/"owner_review_renderer.py").read_text(encoding="utf-8")
  self.assertIn('10-minute repeat permission (not enabled yet)',source)
  self.assertIn('disabled=True',source)
 def test_read_wire_rejects_extra_rights(self):
  with self.assertRaises(ValueError):
   _validate_arguments("read_text_file",{"path":"C:/x","approve_repeat":True})
 def test_read_wire_rejects_noninteger_limit(self):
  with self.assertRaises(ValueError):
   _validate_arguments("read_text_file",{"path":"C:/x","max_bytes":True})
if __name__=="__main__":unittest.main()
