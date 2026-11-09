"""Phase9T installed dependency metadata checks, never installer authorization."""
import pathlib,sys,unittest
from dataclasses import replace
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_environment import (
    installed_snapshot,compare_environment,EnvironmentMismatch)
class EnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap=installed_snapshot()
    def test_python_and_package_metadata(self):
        self.assertIsInstance(self.snap.packages,tuple)
        self.assertEqual(self.snap.python.split(".")[0],"3")
    def test_exact_match_is_not_authorization(self):
        result=compare_environment(self.snap,self.snap)
        self.assertFalse(result["wheel_hashes_verified"])
        self.assertFalse(result["publisher_authenticated"])
        self.assertFalse(result["installation_permitted"])
    def test_python_drift_rejected(self):
        with self.assertRaises(EnvironmentMismatch):
            compare_environment(self.snap,replace(self.snap,python="0.0.0"))
    def test_dependency_drift_rejected(self):
        with self.assertRaises(EnvironmentMismatch):
            compare_environment(self.snap,replace(self.snap,packages=self.snap.packages + (("test-placeholder", "1"),)))
    def test_architecture_drift_rejected(self):
        with self.assertRaises(EnvironmentMismatch):
            compare_environment(self.snap,replace(self.snap,architecture="unknown"))
    def test_forged_dict_rejected(self):
        with self.assertRaises(EnvironmentMismatch):
            compare_environment(dict(self.snap.__dict__),self.snap)
if __name__=="__main__":
    unittest.main()
