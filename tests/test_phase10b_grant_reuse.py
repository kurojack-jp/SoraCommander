"""Phase10B: reusable Owner grants remain exact, scoped, expiring."""
import pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.policy import Policy,Pattern,Grant,Decision
from sora_commander_next.commands import Command
class GrantReuseTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  self.root=str(pathlib.Path(t.name).resolve())
  self.clock_value=100.
  self.clock=lambda:self.clock_value
  self.exe=str(pathlib.Path(sys.executable).resolve())
  self.pattern=Pattern(self.exe,("--version",))
  self.policy=Policy(roots=(self.root,),clock=self.clock)
  self.g=Grant("g1","local-owner",self.root,(self.pattern,),150.)
  self.policy.add_grant_local(self.g)
 def request(self,argv=("--version",),cwd=None):
  return Command(executable=self.exe,argv=argv,cwd=cwd or self.root)
 def test_same_exact_command_is_reused(self):
  for _ in range(3):
   self.assertEqual(self.policy.classify(self.request(),"local-owner"),Decision.SCOPED)
 def test_other_arguments_still_review(self):
  self.assertEqual(self.policy.classify(self.request(("-c","print(1)")),"local-owner"),Decision.REVIEW)
 def test_other_owner_still_review(self):
  self.assertEqual(self.policy.classify(self.request(),"different-owner"),Decision.REVIEW)
 def test_expiry_stops_reuse(self):
  self.clock_value=150.
  self.assertEqual(self.policy.classify(self.request(),"local-owner"),Decision.REVIEW)
 def test_revoke_stops_reuse(self):
  self.policy.revoke_local("g1","local-owner")
  self.assertEqual(self.policy.classify(self.request(),"local-owner"),Decision.REVIEW)
 def test_wrong_owner_cannot_revoke(self):
  with self.assertRaises(PermissionError):
   self.policy.revoke_local("g1","other-owner")
 def test_outside_root_denied(self):
  with tempfile.TemporaryDirectory() as other:
   self.assertEqual(self.policy.classify(self.request(cwd=other),"local-owner"),Decision.DENY)
if __name__=="__main__":unittest.main()
