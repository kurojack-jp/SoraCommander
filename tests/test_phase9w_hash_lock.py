import pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_hash_lock import make_require_hashes
from sora_commander_next.release_environment import EnvironmentMismatch
class TestHashLock(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  import json,hashlib
  self.p=pathlib.Path(t.name);self.w=self.p/"w";self.w.mkdir()
  self.f=self.w/"demo-1.0-py3-none-any.whl";self.f.write_bytes(b"data")
  self.i=self.p/"i.json"
  self.i.write_text(json.dumps([{"filename":self.f.name,"sha256":hashlib.sha256(b"data").hexdigest(),"bytes":4}]))
  self.pins=self.p/"pins.txt";self.pins.write_text("demo==1.0\n")
 def test_valid_hash_lock(self):
  self.assertIn("--hash=sha256:",make_require_hashes(self.w,self.i,self.pins))
 def test_modified_wheel_denied(self):
  self.f.write_bytes(b"changed")
  with self.assertRaises(EnvironmentMismatch):make_require_hashes(self.w,self.i,self.pins)
 def test_missing_pin_denied(self):
  self.pins.write_text("")
  with self.assertRaises(EnvironmentMismatch):make_require_hashes(self.w,self.i,self.pins)
 def test_wrong_version_denied(self):
  self.pins.write_text("demo==2.0")
  with self.assertRaises(EnvironmentMismatch):make_require_hashes(self.w,self.i,self.pins)
 def test_extra_wheel_denied(self):
  (self.w/"other-1.0-py3-none-any.whl").write_bytes(b"x")
  with self.assertRaises(EnvironmentMismatch):make_require_hashes(self.w,self.i,self.pins)
if __name__=="__main__":unittest.main()
