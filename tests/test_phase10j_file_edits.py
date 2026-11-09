import hashlib,pathlib,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.file_edits import replace_text
from sora_commander_next.file_reads import ReadBoundaryError
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
class FileEditTests(unittest.TestCase):
 def setUp(self):
  self.enterContext(canonical_fixture_admission())
 def test_exact_old_hash_replaces(self):
  with inherited_test_temp() as t:
   p=pathlib.Path(t)/"a.txt";p.write_text("old")
   old=hashlib.sha256(b"old").hexdigest()
   self.assertEqual(replace_text(str(p),[t],"new",old)["status"],"REPLACED_LOCAL_DEV")
   self.assertEqual(p.read_text(),"new")
 def test_stale_hash_denied(self):
  with inherited_test_temp() as t:
   p=pathlib.Path(t)/"a.txt";p.write_text("original")
   with self.assertRaises(ReadBoundaryError):
    replace_text(str(p),[t],"new","0"*64)
   self.assertEqual(p.read_text(),"original")
 def test_outside_root_denied(self):
  with inherited_test_temp() as t,inherited_test_temp() as other:
   p=pathlib.Path(other)/"a.txt";p.write_text("old")
   with self.assertRaises(ReadBoundaryError):
    replace_text(str(p),[t],"new",hashlib.sha256(b"old").hexdigest())
if __name__=="__main__":unittest.main()
