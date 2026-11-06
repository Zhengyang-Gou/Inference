"""Ordered submissions, bounded pipe writes and unchanged native event timing."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from joint_scheduling.backend.build import BackendError, PACKAGE
from joint_scheduling.backend.client import Ns3Backend
from joint_scheduling.backend.settings import parse_config
from joint_scheduling.backend.tests.test_backend import backend_available
from joint_scheduling.config import load_config


class Pipe:
    def __init__(self, partial=False):
        self.writes = []
        self.partial = partial

    def write(self, data):
        data = bytes(data)
        count = min(5, len(data)) if self.partial else len(data)
        self.writes.append(data[:count])
        return count


def client(replies, size=128, partial=False):
    backend = Ns3Backend.__new__(Ns3Backend)
    backend.process = SimpleNamespace(poll=lambda: None, stdin=Pipe(partial))
    backend.log_path = Path('unused-test-backend.log')
    backend._read = Mock(side_effect=replies)
    backend._next_id, backend.now_ns = 1, 0
    backend.native_schedule=None;backend._epoch_completions=[]
    backend._callbacks, backend._part_owners = {}, {}
    backend._batch_depth, backend._pending_submissions = 0, []
    backend._submission_commands = backend._submission_batches = backend._barrier_commands = 0
    backend.submission_batch_size = size
    return backend


class SubmissionBatchTests(unittest.TestCase):
    def test_native_calendar_copies_completion_epoch_and_rejects_extra_commands(self):
        backend=client(['OK','TIME 0 1 1','OK'])
        seen=[]
        backend.native_schedule=lambda row:seen.append(row)
        backend.call_at(0,lambda:backend.call_at(.001,lambda:None))
        backend.advance(0)
        self.assertEqual(seen[0]['after_ids'],[])
        self.assertEqual(seen[1]['after_ids'],[1])
        self.assertEqual(seen[1]['command'],'TIMER {id} 1000000')
        seen[1]['after_ids'].append(999)
        self.assertEqual(backend._epoch_completions,[1])
        for command in ('TIMER {id} 1\nCLOSE','ADVANCE {id} 0','TIMER 1 0'):
            with self.assertRaises(BackendError):backend.submit_frozen_event(command,lambda:None)

    def test_nested_batches_keep_ids_and_callbacks_without_advancing_time(self):
        backend = client(['OK'] * 3)
        callbacks = []
        with backend.submission_batch():
            backend.call_at(0, lambda: callbacks.append(1))
            with backend.submission_batch():
                backend.call_at(.001, lambda: callbacks.append(2))
            backend.call_at(.002, lambda: callbacks.append(3))
            self.assertEqual(backend.process.stdin.writes, [])
        self.assertEqual(backend.process.stdin.writes, [
            b'TIMER 1 0\nTIMER 2 1000000\nTIMER 3 2000000\n'])
        self.assertEqual(set(backend._callbacks), {1, 2, 3})
        self.assertEqual(callbacks, [])
        self.assertEqual(backend.now_ns, 0)

    def test_batch_limit_bounds_pending_commands_and_size_one_is_immediate(self):
        for size, batches in ((1, 5), (2, 3)):
            backend = client(['OK'] * 5, size=size)
            with backend.submission_batch():
                for _ in range(5):
                    backend.call_at(0, lambda: None)
                    self.assertLess(len(backend._pending_submissions), size)
            self.assertEqual(len(backend.process.stdin.writes), batches)
            self.assertEqual(backend._read.call_count, 5)

    def test_queries_flush_submissions_before_observing_backend_state(self):
        state = dict(time_ns=0, resources=[], flows=[])
        backend = client(['OK', 'STATS ' + json.dumps(state), 'OK'])
        with backend.submission_batch():
            backend.call_at(0, lambda: None)
            self.assertEqual(backend.network_snapshot(), state)
            backend.call_at(0, lambda: None)
        self.assertEqual(backend.process.stdin.writes, [b'TIMER 1 0\n', b'STATS\n', b'TIMER 2 0\n'])

    def test_one_completion_batches_followup_commands_in_callback_order(self):
        backend = client(['OK', 'TIME 0 1 1', 'OK', 'OK'])
        def complete():
            backend.call_at(.001, lambda: None)
            backend.call_at(.002, lambda: None)
        backend.call_at(0, complete)
        self.assertEqual(backend.advance(0), 1)
        self.assertEqual(backend.process.stdin.writes[-1], b'TIMER 2 1000000\nTIMER 3 2000000\n')
        self.assertEqual(set(backend._callbacks), {2, 3})

    def test_partial_pipe_writes_do_not_truncate_commands(self):
        backend = client(['OK', 'OK'], partial=True)
        with backend.submission_batch():
            backend.call_at(0, lambda: None)
            backend.call_at(0, lambda: None)
        self.assertEqual(b''.join(backend.process.stdin.writes), b'TIMER 1 0\nTIMER 2 0\n')

    def test_body_errors_flush_prior_valid_submissions_and_rejections_propagate(self):
        backend = client(['OK'])
        with self.assertRaisesRegex(ValueError, 'body failed'):
            with backend.submission_batch():
                backend.call_at(0, lambda: None)
                raise ValueError('body failed')
        self.assertEqual(backend.process.stdin.writes, [b'TIMER 1 0\n'])
        backend = client(['BAD'])
        with self.assertRaisesRegex(BackendError, 'rejected'):
            with backend.submission_batch():
                backend.call_at(0, lambda: None)
        self.assertEqual(backend._callbacks, {})

    def test_configuration_and_direct_client_reject_invalid_batch_sizes(self):
        for size in (0, 257, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                parse_config({'rpc_submission_batch_size': size})
            with self.subTest(client_size=size), self.assertRaises(BackendError):
                Ns3Backend(None, 'unused-test-output', submission_batch_size=size)
        self.assertEqual(parse_config({'rpc_submission_batch_size': 1}).rpc_submission_batch_size, 1)


@unittest.skipUnless(backend_available(), 'native backend not built')
class NativeBatchTests(unittest.TestCase):
    def test_batched_and_immediate_submission_keep_native_flow_times_and_stats(self):
        topology = load_config(PACKAGE / 'configs/baseline_smoke.json').topology
        outcomes, summaries = [], []
        with tempfile.TemporaryDirectory() as directory:
            for size in (1, 128):
                output = Path(directory) / str(size)
                events = []
                with Ns3Backend(topology, output, submission_batch_size=size) as backend:
                    def start():
                        for src in (0, 1, 3):
                            backend.send_hosts(src, 2, 8192,
                                lambda src=src: events.append((src, backend.now_ns)))
                        backend.call_at(.000001, lambda: events.append(('timer', backend.now_ns)))
                    with backend.submission_batch():
                        backend.call_at(0, start)
                        # Large logical local transfer exercises the reliable
                        # message split without creating millions of packets.
                        backend.send_hosts(0, 0, 64 * 1024**2 + 13,
                            lambda: events.append(('local', backend.now_ns)))
                    backend.run_until(.01)
                    outcomes.append((events, backend.network_snapshot()))
                summaries.append(json.loads((output / 'backend_rpc_summary.json').read_text()))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(summaries[0]['submission_commands'], summaries[1]['submission_commands'])
        self.assertLess(summaries[1]['synchronous_exchanges'], summaries[0]['synchronous_exchanges'])


if __name__ == '__main__':
    unittest.main()
