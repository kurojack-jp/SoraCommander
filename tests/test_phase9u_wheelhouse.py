import pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_wheelhouse import inventory,verify,WheelhouseMismatch,load_inventory

class TestWheelhouse(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=pathlib.Path(t.name)
        (self.root/"demo-1.0-py3-none-any.whl").write_bytes(b"fixture")
        self.expected=inventory(self.root)
    def test_matching_hashes_are_only_candidate_evidence(self):
        r=verify(self.root,self.expected)
        self.assertEqual(r["wheel_count"],1)
        self.assertFalse(r["install_permitted"])
        self.assertFalse(r["publisher_authenticated"])
    def test_modified_wheel_rejected(self):
        (self.root/"demo-1.0-py3-none-any.whl").write_bytes(b"changed")
        with self.assertRaises(WheelhouseMismatch):
            verify(self.root,self.expected)
    def test_extra_wheel_rejected(self):
        (self.root/"extra-1.0-py3-none-any.whl").write_bytes(b"x")
        with self.assertRaises(WheelhouseMismatch):
            verify(self.root,self.expected)
    def test_missing_wheel_rejected(self):
        (self.root/"demo-1.0-py3-none-any.whl").unlink()
        with self.assertRaises(WheelhouseMismatch):
            verify(self.root,self.expected)
    def test_spurious_nonwheel_rejected(self):
        (self.root/"notes.txt").write_text("hello")
        with self.assertRaises(WheelhouseMismatch):
            inventory(self.root)
    def test_duplicate_json_key_rejected(self):
        with self.assertRaises(WheelhouseMismatch):
            load_inventory('[{"sha256":"a","sha256":"b"}]')
    def test_bad_expected_hash_rejected(self):
        with self.assertRaises(WheelhouseMismatch):
            verify(self.root,[dict(self.expected[0],sha256="f")])
if __name__=="__main__":
    unittest.main()
