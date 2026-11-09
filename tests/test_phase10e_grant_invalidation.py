import pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.policy import Policy,Grant,Pattern,Decision
from sora_commander_next.commands import Command
class GrantInvalidationTests(unittest.TestCase):
 def test_revocation_is_scoped_and_changes_generation(self):
  with tempfile.TemporaryDirectory() as folder:
   exe=str(pathlib.Path(sys.executable).resolve())
   p=Policy(roots=(folder,),clock=lambda:100.)
   cmd=Command(executable=exe,argv=("--version",),cwd=folder)
   pattern=Pattern(exe,("--version",))
   p.add_grant_local(Grant("a","owner-a",folder,(pattern,),200.))
   p.add_grant_local(Grant("b","owner-b",folder,(pattern,),200.))
   self.assertEqual(p.classify(cmd,"owner-a"),Decision.SCOPED)
   before=p.generation
   self.assertEqual(p.revoke_owner_grants_local("owner-a"),1)
   self.assertGreater(p.generation,before)
   self.assertEqual(p.classify(cmd,"owner-a"),Decision.REVIEW)
   self.assertEqual(p.classify(cmd,"owner-b"),Decision.SCOPED)
   self.assertEqual(p.revoke_owner_grants_local("owner-a"),0)
if __name__=="__main__":unittest.main()
