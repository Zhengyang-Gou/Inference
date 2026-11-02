import unittest
from pathlib import Path

from joint_scheduling.backend.runtime import ComputeProfile
from joint_scheduling.config import load_config
from joint_scheduling.experiments.capacity import estimate_capacity


ROOT = Path(__file__).resolve().parents[3]


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.profile = ComputeProfile(ROOT / "joint_scheduling/backend/fixtures/layered_compute_profile.json")
        self.config = load_config(ROOT / "joint_scheduling/configs/baseline_all_off.json")

    def workload(self):
        return dict(schema_version=1, provenance=dict(synthetic=False), requests=[
            dict(request_id="a",arrival_seconds=0,input_tokens=512,output_tokens=3),
            dict(request_id="b",arrival_seconds=1,input_tokens=2048,output_tokens=1)])

    def test_actual_decode_steps_and_round_robin_instance_limits(self):
        result = estimate_capacity(self.workload(), self.profile, self.config)
        expected = sum(self.profile.lookup("decode",t,4)["compute_seconds"] for t in (512,513)) / 2
        self.assertAlmostEqual(result["mean_decode_compute_seconds_per_request"],expected)
        self.assertAlmostEqual(result["mean_prefill_compute_seconds"],.0075)
        self.assertAlmostEqual(result["prefill_compute_bound_rps"],2/.012)
        self.assertEqual(result["mean_kv_bytes_per_request"],512*32768/2)
        self.assertIsNone(result["sustainable_capacity_rps"])
        self.assertEqual(result["status"],"offline_estimate_not_measured")
        demands = {r["resource"]:r["bytes_per_global_request"] for r in result["resource_demands"]}
        self.assertAlmostEqual(sum(v for k,v in demands.items() if k.endswith(":tx")),
                               sum(v for k,v in demands.items() if k.endswith(":rx")))
        self.assertTrue(result["all_standalone_requests_fit_memory"])

    def test_uncovered_tail_is_rejected_and_single_token_needs_no_decode(self):
        workload = self.workload()
        workload["requests"][0].update(input_tokens=4000,output_tokens=200)
        with self.assertRaisesRegex(ValueError,"coverage"):
            estimate_capacity(workload,self.profile,self.config)
        for row in workload["requests"]:
            row["output_tokens"] = 1
        result = estimate_capacity(workload,self.profile,self.config)
        self.assertEqual(result["mean_decode_compute_seconds_per_request"],0)
        self.assertIsNone(result["decode_compute_bound_rps"])


if __name__ == "__main__":
    unittest.main()
