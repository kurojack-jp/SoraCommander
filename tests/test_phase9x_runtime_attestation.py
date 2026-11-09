import pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_runtime_attestation import inspect_binary,review_binary,RuntimeAttestationError
class RuntimeTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  self.p=pathlib.Path(t.name)/"python.exe";self.p.write_bytes(b"sample executable")
 def test_digest_matches_but_install_stays_denied(self):
  evidence=inspect_binary(self.p)
  result=review_binary(evidence,externally_pinned_sha256=evidence.sha256)
  self.assertFalse(result["external_pin_origin_verified"])
  self.assertFalse(result["installation_permitted"])
 def test_modified_binary_is_rejected(self):
  e=inspect_binary(self.p)
  self.p.write_bytes(b"modified")
  with self.assertRaises(RuntimeAttestationError):
   review_binary(inspect_binary(self.p),externally_pinned_sha256=e.sha256)
 def test_wrong_pin_denied(self):
  with self.assertRaises(RuntimeAttestationError):
   review_binary(inspect_binary(self.p),externally_pinned_sha256="0"*64)
 def test_not_binary_denied(self):
  q=self.p.with_suffix(".txt");q.write_bytes(b"x")
  with self.assertRaises(RuntimeAttestationError):inspect_binary(q)
 def test_size_limit_denied(self):
  with self.assertRaises(RuntimeAttestationError):inspect_binary(self.p,max_bytes=2)
if __name__=="__main__":unittest.main()
