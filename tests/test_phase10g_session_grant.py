import pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.policy import Policy,Grant,Pattern,Decision
from sora_commander_next.commands import Command
class SessionGuardTests(unittest.TestCase):
 def test_reusable_permission_fails_closed_when_session_guard_invalidates(self):
  with tempfile.TemporaryDirectory() as folder:
   exe=str(pathlib.Path(sys.executable).resolve())
   p=Policy(roots=(folder,),clock=lambda:100.)
   cmd=Command(executable=exe,argv=("--version",),cwd=folder)
   p.add_grant_local(Grant("g","owner",folder,(Pattern(exe,("--version",)),),700.))
   locked=[False]
   def check():
    if locked[0]:raise PermissionError("session changed")
   p.bind_grant_guard_local("g","owner",check)
   self.assertEqual(p.classify(cmd,"owner"),Decision.SCOPED)
   locked[0]=True
   self.assertEqual(p.classify(cmd,"owner"),Decision.REVIEW)
   locked[0]=False
   self.assertEqual(p.classify(cmd,"owner"),Decision.REVIEW)
 def test_guard_rejected_before_binding(self):
  with tempfile.TemporaryDirectory() as folder:
   exe=str(pathlib.Path(sys.executable).resolve())
   p=Policy(roots=(folder,),clock=lambda:100.)
   p.add_grant_local(Grant("g","owner",folder,(Pattern(exe,("--version",)),),700.))
   with self.assertRaises(PermissionError):
    p.bind_grant_guard_local("g","owner",lambda:(_ for _ in ()).throw(PermissionError()))
if __name__=="__main__":unittest.main()
