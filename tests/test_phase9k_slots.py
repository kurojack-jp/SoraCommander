"""Phase 9K: isolated A/B transitions, no installation or OS changes."""
from types import SimpleNamespace
import unittest
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_slots import LocalSlotSimulator,SlotSimulationError

def preview(start=2,finish=3):
    return SimpleNamespace(status="SIGNED_UPDATE_VALIDATED_INSTALL_AND_ACTIVATION_HOLD",
        from_sequence=start,to_sequence=finish,installation_permitted=False,
        activation_permitted=False,remote_permitted=False)

class OfflineSlotsTests(unittest.TestCase):
    def test_happy_path_requires_explicit_ack_and_fake_health(self):
        sim=LocalSlotSimulator(active="A",sequence=2)
        self.assertEqual(sim.stage(preview()).pending,"B")
        with self.assertRaises(SlotSimulationError):
            sim.simulate_activation()
        self.assertEqual(sim.simulate_activation(owner_ack=True).active,"B")
        self.assertEqual(sim.snapshot().status,"AWAITING_FAKE_HEALTH")
        end=sim.simulate_health(healthy=True)
        self.assertEqual(end.status,"STABLE")
        self.assertEqual(end.sequence,3)
        self.assertIsNone(end.previous)
        self.assertFalse(end.production_authorized)
        self.assertFalse(end.remote_authorized)

    def test_failure_reverts_slot_but_keeps_highwater(self):
        sim=LocalSlotSimulator(active="A",sequence=2)
        sim.stage(preview())
        sim.simulate_activation(owner_ack=True)
        fail=sim.simulate_health(healthy=False)
        self.assertEqual((fail.active,fail.sequence,fail.high_water),("A",2,3))
        self.assertEqual(fail.status,"RECOVERED_OLD_SLOT_SIMULATION")
        sim.reset_after_recovery()
        with self.assertRaises(SlotSimulationError):
            sim.stage(preview())
        self.assertEqual(sim.stage(preview(finish=4)).pending,"B")

    def test_abort_staging_does_not_change_current_version(self):
        sim=LocalSlotSimulator(sequence=2)
        sim.stage(preview())
        snap=sim.abort_pending()
        self.assertEqual((snap.active,snap.sequence,snap.high_water),("A",2,2))
        self.assertIsNone(snap.pending)

    def test_replay_old_wrong_or_unverified_preview_is_rejected(self):
        sim=LocalSlotSimulator(sequence=2,high_water=5)
        for p in (preview(finish=3),preview(start=1,finish=6),
                  preview(start=2,finish=5),
                  SimpleNamespace(status="DENIED",from_sequence=2,to_sequence=6,
                    installation_permitted=False,activation_permitted=False,
                    remote_permitted=False),
                  SimpleNamespace(status="SIGNED_UPDATE_VALIDATED_INSTALL_AND_ACTIVATION_HOLD",
                    from_sequence=2,to_sequence=6,installation_permitted=True,
                    activation_permitted=False,remote_permitted=False)):
            with self.subTest(p=p),self.assertRaises(SlotSimulationError):
                sim.stage(p)

    def test_no_double_switch_without_health(self):
        sim=LocalSlotSimulator(sequence=2)
        sim.stage(preview())
        with self.assertRaises(SlotSimulationError):
            sim.stage(preview())
        sim.simulate_activation(owner_ack=True)
        with self.assertRaises(SlotSimulationError):
            sim.stage(preview(finish=4))
        with self.assertRaises(SlotSimulationError):
            sim.abort_pending()

    def test_cannot_fake_health_before_activation(self):
        sim=LocalSlotSimulator(sequence=2)
        with self.assertRaises(SlotSimulationError):
            sim.simulate_health(healthy=True)
        sim.stage(preview())
        with self.assertRaises(SlotSimulationError):
            sim.simulate_health(healthy=False)

    def test_strict_boolean_guards_and_invalid_initial_state(self):
        for bad in (True,0,"2",-1):
            with self.subTest(bad=bad),self.assertRaises(SlotSimulationError):
                LocalSlotSimulator(sequence=bad)
        sim=LocalSlotSimulator(sequence=2)
        sim.stage(preview())
        with self.assertRaises(SlotSimulationError):
            sim.simulate_activation(owner_ack=1)
        sim.simulate_activation(owner_ack=True)
        with self.assertRaises(SlotSimulationError):
            sim.simulate_health(healthy=1)

    def test_not_a_live_installer(self):
        sim=LocalSlotSimulator(sequence=2)
        self.assertFalse(hasattr(sim,"install"))
        self.assertFalse(hasattr(sim,"start_service"))
        self.assertFalse(hasattr(sim,"enable_remote"))
        self.assertFalse(sim.snapshot().production_authorized)

if __name__=="__main__":
    unittest.main()
