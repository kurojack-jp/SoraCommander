"""Inert Broker lifecycle state tests; no subprocess, IPC or OS access."""
import unittest
from sora_commander_next.offline_broker_lifecycle import (
    Snapshot, advance, describe, LifecycleRejected,
)


class BrokerLifecycleTests(unittest.TestCase):
    def test_normal_start_shutdown_only_changes_data(self):
        state = Snapshot()
        self.assertEqual(state.state, "STOPPED")
        state = advance(state, "begin")
        self.assertEqual((state.state, state.epoch), ("STARTING", 1))
        state = advance(state, "ready")
        self.assertEqual(state.state, "READY")
        state = advance(state, "disconnect")
        self.assertEqual(state.state, "READY")
        self.assertFalse(describe(state)["jobs_replayed"])
        self.assertFalse(describe(state)["execution_authorized"])
        state = advance(state, "drain")
        state = advance(state, "stopped")
        self.assertEqual(state.state, "STOPPED")
        self.assertEqual(advance(state, "begin").epoch, 2)

    def test_duplicate_start_and_unexpected_ready_refused(self):
        start = advance(Snapshot(), "begin")
        with self.assertRaises(LifecycleRejected):
            advance(start, "begin")
        with self.assertRaises(LifecycleRejected):
            advance(Snapshot(), "ready")
        with self.assertRaises(LifecycleRejected):
            advance(Snapshot(), "stopped")

    def test_fault_never_auto_recovers(self):
        broken = advance(advance(Snapshot(), "begin"), "fault")
        self.assertEqual(broken.state, "FAULTED")
        for event in ("begin", "ready", "stopped", "disconnect", "drain"):
            with self.subTest(event=event):
                with self.assertRaises(LifecycleRejected):
                    advance(broken, event)

    def test_no_authority_in_snapshot_or_output(self):
        with self.assertRaises(LifecycleRejected):
            Snapshot(execution_enabled=True)
        with self.assertRaises(LifecycleRejected):
            Snapshot(epoch=True)
        with self.assertRaises(LifecycleRejected):
            Snapshot(connections=2)
        with self.assertRaises(LifecycleRejected):
            advance(Snapshot(), "execute")
        self.assertFalse(describe(Snapshot())["broker_process_verified"])

    def test_connection_observation_requires_ready_and_is_single_slot(self):
        started = advance(Snapshot(), "begin")
        with self.assertRaises(LifecycleRejected):
            advance(started, "connect")
        ready = advance(started, "ready")
        self.assertTrue(describe(ready)["accepting_connections"])
        connected = advance(ready, "connect")
        self.assertEqual(connected.connections, 1)
        self.assertFalse(describe(connected)["accepting_connections"])
        self.assertFalse(describe(connected)["client_identity_verified"])
        with self.assertRaises(LifecycleRejected):
            advance(connected, "connect")
        disconnected = advance(connected, "disconnect")
        self.assertEqual(disconnected.connections, 0)
        self.assertEqual(disconnected.epoch, ready.epoch)
        self.assertEqual(advance(disconnected, "connect").connections, 1)

    def test_drain_fault_clear_connections_and_invalid_snapshot_denied(self):
        ready = advance(advance(Snapshot(), "begin"), "ready")
        connected = advance(ready, "connect")
        drained = advance(connected, "drain")
        self.assertEqual(drained.connections, 0)
        self.assertFalse(describe(drained)["accepting_connections"])
        with self.assertRaises(LifecycleRejected):
            advance(drained, "connect")
        self.assertEqual(advance(drained, "stopped").state, "STOPPED")
        faulted = advance(connected, "fault")
        self.assertEqual(faulted.connections, 0)
        with self.assertRaises(LifecycleRejected):
            Snapshot(state="STOPPED", connections=1)

    def test_disconnected_state_does_not_change_epoch(self):
        state = advance(advance(Snapshot(), "begin"), "ready")
        previous = state.epoch
        for _ in range(3):
            state = advance(state, "disconnect")
        self.assertEqual(state.epoch, previous)
        self.assertEqual(state.state, "READY")


if __name__ == "__main__":
    unittest.main()
