"""Phase9L filesystem A/B crash simulation, only tempfile.TemporaryDirectory."""
import pathlib
import tempfile
import unittest
import sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_sandbox import DisposableAB,SandboxUpdateError

class Phase9LTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="sora9l-")
        self.addCleanup(temp.cleanup)
        self.root=pathlib.Path(temp.name)/"disposable"
        self.s=DisposableAB.create(self.root)

    def test_staging_survives_new_object_then_switches(self):
        self.s.stage(2,validated_preview=True)
        reopened=DisposableAB(self.root)
        self.assertEqual(reopened.snapshot()["phase"],"STAGED")
        reopened.switch(owner_simulated_ack=True)
        self.assertEqual(DisposableAB(self.root).health(healthy=True)["sequence"],2)
        self.assertEqual((self.root/"A"/"payload.txt").read_text("ascii"),"TEST_VERSION_1\n")

    def test_simulated_crash_during_staging_restores_state(self):
        self.s.stage(2,validated_preview=True)
        r=DisposableAB(self.root).recover_after_crash()
        self.assertEqual((r["active"],r["phase"],r["high_water"]),("A","STABLE",1))

    def test_simulated_crash_after_switch_recovers_previous(self):
        self.s.stage(2,validated_preview=True)
        self.s.switch(owner_simulated_ack=True)
        recovered=DisposableAB(self.root).recover_after_crash()
        self.assertEqual((recovered["active"],recovered["sequence"],recovered["high_water"]),("A",1,2))
        self.s.finalize_recovery()
        with self.assertRaises(SandboxUpdateError):
            self.s.stage(2,validated_preview=True)
        self.assertEqual(self.s.stage(3,validated_preview=True)["pending_sequence"],3)

    def test_explicit_failure_restores_a_and_preserves_b(self):
        self.s.stage(3,validated_preview=True)
        self.s.switch(owner_simulated_ack=True)
        fail=self.s.health(healthy=False)
        self.assertEqual(fail["active"],"A")
        self.assertEqual(fail["high_water"],3)
        self.assertEqual((self.root/"B"/"payload.txt").read_text("ascii"),"TEST_VERSION_3\n")

    def test_invalid_preview_cannot_stage(self):
        for v in (1,0,-1,True,2.0,"2"):
            with self.subTest(v=v), self.assertRaises(SandboxUpdateError):
                self.s.stage(v,validated_preview=True)
        with self.assertRaises(SandboxUpdateError):
            self.s.stage(2)

    def test_no_switch_without_exact_ack(self):
        self.s.stage(2,validated_preview=True)
        for value in (None,1,False,"yes"):
            with self.subTest(value=value),self.assertRaises(SandboxUpdateError):
                self.s.switch(owner_simulated_ack=value)

    def test_existing_root_cannot_be_initialized_or_modified(self):
        with self.assertRaises(SandboxUpdateError):
            DisposableAB.create(self.root)
        self.assertEqual(self.s.snapshot()["sequence"],1)

    def test_missing_sentinel_fails_closed(self):
        (self.root/".sora-phase9l-disposable").unlink()
        with self.assertRaises(SandboxUpdateError):
            DisposableAB(self.root)

    def test_injected_directory_denies(self):
        (self.root/"unwanted").mkdir()
        with self.assertRaises(SandboxUpdateError):
            DisposableAB(self.root)

    def test_corrupted_journal_denies(self):
        (self.root/"state.json").write_text("{}","ascii")
        with self.assertRaises(SandboxUpdateError):
            DisposableAB(self.root)

    def test_after_healthy_no_old_replay(self):
        self.s.stage(4,validated_preview=True)
        self.s.switch(owner_simulated_ack=True)
        self.s.health(healthy=True)
        with self.assertRaises(SandboxUpdateError):
            self.s.stage(3,validated_preview=True)

    def test_refuses_forged_directories(self):
        with self.assertRaises(SandboxUpdateError):
            DisposableAB(self.root.parent)

if __name__=="__main__":
    unittest.main()
