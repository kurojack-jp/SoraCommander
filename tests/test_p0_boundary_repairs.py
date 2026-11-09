"""Regression specifications: injected peers/stores only, no native authority."""
import unittest
from hashlib import sha256
from unittest.mock import Mock

from sora_commander_next.broker_host import BrokerHost, BrokerUnavailable, decode_frame, encode_frame
from sora_commander_next.broker_runtime import BrokerRuntime
from sora_commander_next.broker_protocol import encode_result_page_response, ProtocolRejected
from sora_commander_next.protected_ledger_client import ProtectedLedgerClient, LedgerCallBinding
from test_protected_ledger_client import command
from test_broker_result_page_wire import page, RID, SID
from sora_commander_next.source_review import SourcePolicy, SourceReviewWorkflow, SourceConflict
from test_sol_broker_vertical import AtomicStore, Owner, ALLOW, TARGET


class ReplyBindingTests(unittest.TestCase):
    def call(self, method, result):
        sent = []
        def request(raw):
            envelope = decode_frame(raw)
            sent.append(envelope)
            return encode_frame({k: envelope[k] for k in ('v', 'action', 'call_id')} | {'result': result})
        host = BrokerHost(Mock(request=request))
        value = getattr(host, method)(request_id=RID)
        self.assertEqual(len(sent), 1)
        return value

    def test_command_reply_cannot_replace_original_request_id(self):
        for method in ('run_command', 'run_command_wait'):
            with self.subTest(method=method):
                value = self.call(method, {'status': 'RUNNING', 'request_id': 'f' * 32})
                self.assertEqual(value['status'], 'OUTCOME_UNKNOWN')
                self.assertEqual(value['request_id'], RID)
                self.assertIs(value['auto_retry'], False)

    def test_query_and_source_reply_reject_other_request(self):
        for method in ('request_status', 'source_review_status', 'cancel_source_review'):
            with self.subTest(method=method):
                value = self.call(method, {'status': 'COMPLETED', 'request_id': 'f' * 32})
                self.assertEqual(value['status'], 'OUTCOME_UNKNOWN')
                self.assertNotIn('request_id', value)

    def test_exact_request_and_requestless_refusal_remain_usable(self):
        for result in ({'status': 'RUNNING', 'request_id': RID}, {'status': 'UNAVAILABLE'}):
            self.assertEqual(self.call('run_command', result)['status'], result['status'])


class LedgerAmbiguityTests(unittest.TestCase):
    def setUp(self):
        self.binding = LedgerCallBinding('S-1-5-21-1-2-3-1001', (5, 6), 1,
                                        '2' * 32, '3' * 32, '7' * 64, 1)
        self.transport = Mock(exchange=Mock(return_value=b'not-a-reply'))
        self.client = ProtectedLedgerClient(_transport=self.transport, _clock_ms=lambda: 100)

    def consume(self, observer):
        return self.client.consume_once(command(), operation_id='8' * 32,
                                       binding=self.binding, current_binding=observer)

    def test_post_send_owner_error_is_unknown_and_never_replayed(self):
        observer = Mock(side_effect=[self.binding, BrokerUnavailable('owner observation failed')])
        result = self.consume(observer)
        self.assertEqual(result.observation, 'OUTCOME_UNKNOWN')
        self.assertEqual(result.admission, 'UNAVAILABLE')
        self.assertEqual(self.consume(lambda: self.binding).observation, 'DATA_IGNORED')
        self.transport.exchange.assert_called_once()

    def test_observer_error_is_not_transport_unavailability(self):
        result = self.consume(Mock(side_effect=BrokerUnavailable('observer unavailable')))
        self.assertEqual(result.observation, 'OUTCOME_UNKNOWN')
        self.transport.exchange.assert_not_called()

    def test_transport_known_not_sent_remains_unavailable(self):
        self.transport.exchange.side_effect = BrokerUnavailable('not sent')
        self.assertEqual(self.consume(lambda: self.binding).observation, 'UNAVAILABLE')
        self.assertEqual(self.consume(lambda: self.binding).observation, 'DATA_IGNORED')
        self.transport.exchange.assert_called_once()

    def test_timeout_and_interrupt_cannot_rearm(self):
        for error in (TimeoutError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.transport.exchange.side_effect = error
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.consume(lambda: self.binding)
                else:
                    self.assertEqual(self.consume(lambda: self.binding).observation, 'OUTCOME_UNKNOWN')
                self.assertEqual(self.consume(lambda: self.binding).observation, 'DATA_IGNORED')
                self.transport.exchange.assert_called_once()


class CompletionEvidenceTests(unittest.TestCase):
    def runtime(self, result):
        manager = Mock(read_live_page=Mock(return_value=result))
        runtime = BrokerRuntime(expected_executable='C:/fixed.exe', expected_cwd='D:/repo',
                                owner_binding='owner', owner_guard=Mock(),
                                clock=lambda: 100, manager=manager)
        runtime.sessions[RID] = SID
        return runtime, manager

    def test_runtime_rejects_incomplete_completed_capture(self):
        for flag in ('output_incomplete', 'overflow'):
            with self.subTest(flag=flag):
                runtime, manager = self.runtime(page(**{flag: True}))
                result = runtime.query_result_page(RID)
                self.assertEqual(result, {'request_id': RID, 'status': 'OUTCOME_UNKNOWN'})
                manager.start.assert_not_called()

    def test_wire_rejects_incomplete_completed_capture(self):
        for flag in ('output_incomplete', 'overflow'):
            with self.subTest(flag=flag), self.assertRaises(ProtocolRejected):
                encode_result_page_response(page(**{flag: True}))

    def test_complete_page_and_explicit_incomplete_state_remain_readable(self):
        for result in (page(), page(status='OUTPUT_INCOMPLETE', output_incomplete=True)):
            runtime, manager = self.runtime(result)
            self.assertEqual(runtime.query_result_page(RID), result)
            self.assertIsInstance(encode_result_page_response(result), bytes)
            manager.start.assert_not_called()


class PostCommitSourceTests(unittest.TestCase):
    def test_postcommit_snapshot_conflict_is_unknown_and_halts(self):
        store, owner = AtomicStore(), Owner()
        workflow = SourceReviewWorkflow(policy=SourcePolicy((ALLOW,)), store=store,
                                        owner=owner, clock=lambda: 10.0)
        content = 'x = 1\n'
        args = dict(path=TARGET, content=content,
                    expected_new_sha256=sha256(content.encode()).hexdigest(), request_id=RID)
        self.assertEqual(workflow.submit('request_source_create', args)['status'], 'REVIEW')
        commit = store.commit
        def committed_then_observation_fails(*params):
            result = commit(*params)
            store.snapshot = Mock(side_effect=SourceConflict('postcommit observation denied'))
            return result
        store.commit = committed_then_observation_fails
        result = workflow.review_local(RID)
        self.assertEqual(store.writes, 1)
        self.assertEqual(result['status'], 'OUTCOME_UNKNOWN')
        self.assertIs(result['auto_retry'], False)
        self.assertIs(result['auto_rollback'], False)
        self.assertEqual(workflow.review_local(RID)['status'], 'OUTCOME_UNKNOWN')
        self.assertEqual(store.writes, 1)
        self.assertEqual(workflow.submit('request_source_create', {**args, 'request_id': 'e' * 32}),
                         {'status': 'DENIED', 'error': 'ReviewAdmissionStoppedOrFull'})

    def test_precommit_conflict_is_denied_with_zero_writes(self):
        store = AtomicStore()
        workflow = SourceReviewWorkflow(policy=SourcePolicy((ALLOW,)), store=store,
                                        owner=Owner(), clock=lambda: 10.0)
        args = dict(path=TARGET, content='x', expected_new_sha256=sha256(b'x').hexdigest(), request_id=RID)
        workflow.submit('request_source_create', args)
        store.commit = Mock(side_effect=SourceConflict('no mutation'))
        self.assertEqual(workflow.review_local(RID)['status'], 'DENIED')
        self.assertEqual(store.writes, 0)


if __name__ == '__main__':
    unittest.main()
