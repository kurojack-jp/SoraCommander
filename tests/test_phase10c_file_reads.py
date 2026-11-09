"""Option B real read/list IO; production admission tested separately."""
import pathlib,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.file_reads import read_text_file,list_folder,ReadBoundaryError
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
class ReadOnlyTests(unittest.TestCase):
 def setUp(self):
  self.root=pathlib.Path(self.enterContext(inherited_test_temp()))/"allowed";self.root.mkdir()
  self.enterContext(mock_read_boundary(self.root,("hello.txt","missing")))
  self.file=self.root/"hello.txt";self.file.write_text("hello!",encoding="utf-8")
 def test_read_needs_no_command_execution(self):
  self.assertEqual(read_text_file(str(self.file),[str(self.root)])["content"],"hello!")
 def test_list_files(self):
  self.assertEqual(list_folder(str(self.root),[str(self.root)])["entries"][0]["name"],"hello.txt")
 def test_outside_root_denied(self):
  with self.assertRaises(ReadBoundaryError):read_text_file(str(ROOT/"README.md"),[str(self.root)])
 def test_file_size_bound(self):
  with self.assertRaises(ReadBoundaryError):read_text_file(str(self.file),[str(self.root)],max_bytes=2)
 def test_binary_denied(self):
  self.file.write_bytes(b"\xff\xfe")
  with self.assertRaises(ReadBoundaryError):read_text_file(str(self.file),[str(self.root)])
 def test_mock_undeclared_link_denied(self):
  # Abstract registry rejection only; real link policy has separate tests.
  p=self.root/"link.txt"
  with self.assertRaises(ReadBoundaryError):read_text_file(str(p),[str(self.root)])
 def test_missing_denied(self):
  with self.assertRaises(FileNotFoundError):read_text_file(str(self.root/"missing"),[str(self.root)])
if __name__=="__main__":unittest.main()
