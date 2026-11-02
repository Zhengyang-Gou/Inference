import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from joint_scheduling.backend.runtime import ComputeProfile, load_workload
from joint_scheduling.workload.trace import read_trace, scale_trace, profile_coverage


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "azure.csv"
        self.path.write_text("TIMESTAMP,ContextTokens,GeneratedTokens\n"
                             "2023-11-16 00:00:00.000000001,512,64\n"
                             "2023-11-16 00:00:01.000000001,2048,3\n"
                             "2023-11-16 00:00:02.000000001,17,700\n"
                             "2023-11-16 00:00:03.000000002,9000,1\n")

    def test_native_and_scaled_replay_preserve_pairs_and_nanoseconds(self):
        source = read_trace(self.path, "azure", "https://example.test/trace")
        original = copy.deepcopy(source)
        native = scale_trace(source)
        self.assertEqual(native["requests"][-1]["arrival_seconds"], 3.000000001)
        self.assertFalse(native["provenance"]["synthetic"])
        first, second = scale_trace(source, 8), scale_trace(source, 16)
        self.assertEqual(source, original)
        for workload, rate in ((first, 8), (second, 16)):
            self.assertEqual([(r["input_tokens"], r["output_tokens"]) for r in workload["requests"]],
                             [(512,64),(2048,3),(17,700),(9000,1)])
            self.assertLess(workload["requests"][-1]["arrival_seconds"], 4/rate)
            self.assertEqual(workload["provenance"]["actual_selected_rate_rps"], rate)
            self.assertEqual(workload["provenance"]["clipped_requests"], 0)
        self.assertEqual(first["provenance"]["paired_lengths_sha256"], second["provenance"]["paired_lengths_sha256"])
        destination = self.root / "workload.json"
        destination.write_text(json.dumps(first))
        self.assertEqual(load_workload(destination)[0], first["requests"])

    def test_time_prefix_is_explicit_and_never_repeats_or_clips_tokens(self):
        source = read_trace(self.path, "azure")
        selected = scale_trace(source, 8, duration_seconds=.25)
        self.assertEqual(len(selected["requests"]), 2)
        self.assertEqual(selected["provenance"]["time_window_excluded_requests"], 2)
        self.assertEqual(selected["requests"][1]["input_tokens"], 2048)
        with self.assertRaisesRegex(ValueError, "more data"):
            scale_trace(source, 8, duration_seconds=1)

    def test_hash_validation_and_invalid_lengths(self):
        expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
        read_trace(self.path, "azure", expected_sha256=expected)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            read_trace(self.path, "azure", expected_sha256="0"*64)
        self.path.write_text("TIMESTAMP,ContextTokens,GeneratedTokens\n2023-11-16 00:00:00,12,0\n")
        with self.assertRaises(ValueError):
            read_trace(self.path, "azure")

    def test_mooncake_milliseconds_ties_and_synthetic_source_declaration(self):
        path = self.root / "moon.jsonl"
        path.write_text("\n".join(json.dumps(dict(timestamp=t,input_length=i,output_length=o,hash_ids=[]))
                                   for t,i,o in [(30535,6472,26),(27482,6955,52),(30535,8000,2)]))
        source = read_trace(path, "mooncake", source_synthetic=True)
        workload = scale_trace(source)
        self.assertEqual([r["input_tokens"] for r in workload["requests"]], [6955,6472,8000])
        self.assertEqual([r["arrival_seconds"] for r in workload["requests"]], [0,3.053,3.053])
        self.assertTrue(workload["provenance"]["synthetic"])
        self.assertFalse(workload["provenance"]["prefix_reuse_enabled_by_import"])

    def test_coverage_checks_decode_tail_and_does_not_discard_single_token_requests(self):
        path = Path(__file__).resolve().parents[2] / "backend/fixtures/layered_compute_profile.json"
        profile = ComputeProfile(path)
        workload = scale_trace(read_trace(self.path, "azure"))
        report = profile_coverage(workload, profile)
        self.assertEqual(report["covered_count"], 3)
        workload["requests"] = [dict(request_id="tail",input_tokens=4000,output_tokens=200,arrival_seconds=0)]
        self.assertEqual(profile_coverage(workload, profile)["out_of_coverage_count"], 1)
        workload["requests"][0]["output_tokens"] = 1
        self.assertEqual(profile_coverage(workload, profile)["out_of_coverage_count"], 0)


if __name__ == "__main__":
    unittest.main()
