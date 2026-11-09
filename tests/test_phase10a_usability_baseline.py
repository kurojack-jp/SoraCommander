"""Phase10A: usability baseline without relaxing security or live actions."""
from pathlib import Path
import tempfile,unittest,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.policy import Policy,Pattern,Decision
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
class UsabilityBaseline(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory()
  self.addCleanup(self.tmp.cleanup)
  self.cwd=str(Path(self.tmp.name).resolve())
  self.python=str(Path(sys.executable).resolve())
  self.safe=Pattern(self.python,("--version",))
  self.policy=Policy(roots=(self.cwd,),auto=(self.safe,))
 def request(self,argv):
  return Command(executable=self.python,argv=argv,cwd=self.cwd)
 def test_known_version_query_auto(self):
  self.assertEqual(self.policy.classify(self.request(("--version",)),"local-owner"),Decision.AUTO)
 def test_normal_read_only_script_still_needs_review(self):
  self.assertEqual(self.policy.classify(self.request(("-c","print('hello')")),"local-owner"),Decision.REVIEW)
 def test_unreviewed_script_is_not_executed(self):
  called=[]
  def worker(request):
   called.append(request)
   return {"status":"EXECUTED"}
  h=Host(policy=self.policy,worker=worker)
  result=h.run_command(executable=self.python,argv=["-c","print('hello')"],cwd=self.cwd)
  self.assertEqual(result["status"],"REVIEW")
  self.assertEqual(called,[])
 def test_outside_root_is_denied(self):
  with tempfile.TemporaryDirectory() as other:
   q=Command(executable=self.python,argv=("--version",),cwd=other)
   self.assertEqual(self.policy.classify(q,"local-owner"),Decision.DENY)
if __name__=="__main__":unittest.main()
