import unittest

from joint_scheduling.evaluation.pressure import summarize_pressure, summarize_pressure_states


def request(name, arrival, done=None, slo=True):
    return dict(request_id=name, arrived_at_seconds=arrival, completed_at_seconds=done,
        failed_at_seconds=None, status="completed" if done is not None else "incomplete",
        slo_met=slo if done is not None else None, ttft_slo_seconds=1, itl_slo_seconds=1)


def token(name, at, index=1):
    return dict(request_id=name,returned_at_seconds=at,token_index=index)


class PressureTests(unittest.TestCase):
    def test_boundaries_carry_in_and_backlog_conservation(self):
        rows=[request('carry',0,1),request('a',1,2),request('b',1.5),request('right',2)]
        result=summarize_pressure(rows,[token('carry',0.2),token('a',1),token('a',1.8,2),token('b',1.6)],1,2,2)
        self.assertEqual(result['measured_arrivals'],2)
        self.assertEqual(result['measured_completions'],1)
        self.assertEqual(result['backlog_start'],1)
        self.assertEqual(result['backlog_end'],2)
        self.assertEqual(result['output_tokens_per_second'],3)
        self.assertEqual(result['decode_tokens_per_second'],1)
        self.assertAlmostEqual(result['mean_outstanding'],1.5)
        self.assertEqual(result['peak_outstanding'],2)

    def test_unfinished_deadlines_are_misses_or_explicitly_censored(self):
        rows=[request('pass',0,0.5),request('no_first',0),request('stalled',0),request('unknown',1.9)]
        result=summarize_pressure(rows,[token('pass',0.1),token('stalled',0.1),token('unknown',1.95)],0,2,2)
        self.assertEqual(result['cohort_slo_passed'],1)
        self.assertEqual(result['cohort_slo_missed'],2)
        self.assertEqual(result['cohort_slo_unknown'],1)
        self.assertEqual(result['cohort_slo_fraction_lower'],0.25)
        self.assertEqual(result['cohort_slo_fraction_upper'],0.5)

    def test_queue_integration_uses_held_state_and_requires_full_coverage(self):
        def state(at,value):
            return dict(time_seconds=at,**{key:value for key in ('prefill_queue_length','decode_queue_length',
                'kv_inflight_count','kv_inflight_bytes','collective_inflight_count','collective_inflight_bytes')})
        rows=[state(0,1),state(1,3),state(1,4),state(2,9)]
        result=summarize_pressure_states(rows,0.5,2)
        self.assertEqual(result['mean']['decode_queue_length'],3)
        self.assertEqual(result['maximum']['decode_queue_length'],4)
        with self.assertRaises(ValueError):
            summarize_pressure_states(rows[:-1],0,3)
        with self.assertRaises(ValueError):
            summarize_pressure_states(rows[1:],0,2)
        with self.assertRaises(ValueError):
            summarize_pressure([],[],1,2,1)
