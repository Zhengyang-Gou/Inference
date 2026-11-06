import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import zstandard

from joint_scheduling.cli import main
from joint_scheduling.workload.freeinference import TraceImportError
from joint_scheduling.workload.inputs import load_workload
from joint_scheduling.workload.trace import read_trace, scale_trace


def request(start=1000, prompt=256, completion=8, **extra):
    return dict(start_ms=start, provider_tokens=dict(prompt=prompt, completion=completion, cached=240),
                model="source-model", step=0, turn_index=0, step_trigger="user", tool_calls=[], **extra)


def session(sid, requests, week="2026-05-17_2026-05-23"):
    return dict(session_id=sid, week=week, day="2026-05-17", requests=requests)


class FreeInferenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "trace.jsonl"

    def write(self, sessions, path=None):
        path = path or self.path
        path.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in sessions) + "\n", encoding="utf-8")
        return path

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(["import-trace", "--trace", str(self.path), "--format", "freeinference", *map(str, args)])

    def test_nested_sessions_order_pairs_lineage_and_full_cached_prompt(self):
        child = session("child", [request(2000, 300, 6)])
        child.pop("week")
        child.pop("day")
        parent = request(1000, 512, 12)
        parent["tool_calls"] = [dict(arguments={"secret": "never imported"}, subagent=child)]
        self.write([session("root", [parent, request(3000)]), session("other", [request(2000, 42, 1)])])
        source = read_trace(self.path, "freeinference")
        workload = scale_trace(source)
        self.assertEqual([(r["input_tokens"], r["output_tokens"]) for r in workload["requests"]],
                         [(512, 12), (300, 6), (42, 1), (256, 8)])
        self.assertEqual([r["arrival_seconds"] for r in workload["requests"]], [0, 1, 1, 2])
        lineage = workload["trace_metadata"]["requests"]
        child_meta = lineage[workload["requests"][1]["request_id"]]
        self.assertEqual((child_meta["session_id"], child_meta["root_session_id"], child_meta["parent_session_id"]),
                         ("child", "root", "root"))
        self.assertEqual((child_meta["parent_step"], child_meta["parent_tool_call_index"]), (0, 0))
        self.assertEqual(child_meta["provider_cached_tokens"], 240)
        self.assertEqual(source["audit"]["nested_session_count"], 1)
        self.assertFalse(workload["provenance"]["session_dependencies_modeled"])
        self.assertFalse(workload["provenance"]["prefix_hashes_imported"])
        self.assertNotIn("never imported", json.dumps(workload))
        self.assertTrue(all("prefix_blocks" not in r for r in workload["requests"]))
        output = self.root / "native.json"
        output.write_text(json.dumps(workload))
        self.assertEqual(load_workload(output)[0], workload["requests"])

    def test_explicit_skip_audits_all_bad_pairs_and_preserves_source_time_axis(self):
        self.write([session("root", [request(1000, completion=0), request(2000), request(4000, prompt=None)])])
        with self.assertRaises(TraceImportError) as caught:
            read_trace(self.path, "freeinference")
        self.assertEqual(caught.exception.audit["rejected_request_count"], 2)
        source = read_trace(self.path, "freeinference", invalid_requests="skip")
        workload = scale_trace(source)
        self.assertEqual(workload["requests"][0]["arrival_seconds"], 1)
        self.assertEqual(workload["provenance"]["source_duration_ns"], 3000000001)
        self.assertEqual(workload["provenance"]["source_request_count"], 3)
        self.assertEqual(workload["provenance"]["dropped_requests"], 2)
        self.assertEqual(source["audit"]["rejection_reason_counts"],
                         {"invalid_completion_tokens": 1, "missing_prompt_tokens": 1})

    def test_rejected_parent_does_not_lose_valid_subagent(self):
        parent = request(completion=None)
        parent["tool_calls"] = [dict(subagent=session("child", [request(2000)]))]
        self.write([session("root", [parent])])
        source = read_trace(self.path, "freeinference", invalid_requests="skip")
        self.assertEqual(source["records"][0]["metadata"]["parent_session_id"], "root")
        self.assertEqual(source["audit"]["source_request_count"], 2)

    def test_invalid_time_types_and_lengths_are_not_coerced_or_fabricated(self):
        self.write([session("root", [request(True), request(-1), request(float("inf")),
                                     request(prompt=True), request(completion=1.5),
                                     request(prompt=0), request(5000)])])
        source = read_trace(self.path, "freeinference", invalid_requests="skip")
        self.assertEqual(len(source["records"]), 1)
        self.assertEqual(source["audit"]["rejected_request_count"], 6)
        self.assertEqual(source["audit"]["rejection_reason_counts"]["invalid_start_ms"], 3)

    def test_fractional_epoch_milliseconds_preserve_nanoseconds(self):
        data = json.dumps(session("root", [request(), request()]))
        data = data.replace('"start_ms": 1000', '"start_ms": 1778977459236.000001', 1)
        data = data.replace('"start_ms": 1000', '"start_ms": 1778977459236.000002', 1)
        self.path.write_text(data)
        source = read_trace(self.path, "freeinference")
        self.assertEqual(source["provenance"]["source_origin_ns"], 1778977459236000001)
        self.assertEqual(source["records"][1]["source_time_ns"], 1)

    def test_streamed_zstd_multiple_frames_and_truncation(self):
        compressor = zstandard.ZstdCompressor(write_content_size=False)
        chunks = [json.dumps(session("会话" + str(i), [request(1000+i)])).encode() + b"\n" for i in range(2)]
        path = self.root / "trace.jsonl.zst"
        data = b"".join(compressor.compress(c) for c in chunks)
        path.write_bytes(data)
        source = read_trace(path, "freeinference")
        self.assertEqual(len(source["records"]), 2)
        self.assertEqual(source["provenance"]["source_sha256"], hashlib.sha256(data).hexdigest())
        path.write_bytes(data[:-1])
        with self.assertRaisesRegex(ValueError, "truncated|invalid zstd"):
            read_trace(path, "freeinference")

    def test_directory_manifest_identity_and_week_scoped_session_ids(self):
        source_dir = self.root / "sources"
        source_dir.mkdir()
        self.write([session("same-id", [request(3000)])], source_dir / "b.jsonl")
        self.write([session("same-id", [request(1000)], "2026-05-24_2026-05-30")], source_dir / "a.jsonl")
        source = read_trace(source_dir, "freeinference")
        self.assertEqual(source["provenance"]["source_files"][0]["path"], "a.jsonl")
        digest = source["provenance"]["source_sha256"]
        self.assertEqual(read_trace(source_dir, "freeinference", expected_sha256=digest), source)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            read_trace(source_dir, "freeinference", expected_sha256="0"*64)
        self.write([session("same-id", [request(3000)])], source_dir / "c.jsonl")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            read_trace(source_dir, "freeinference")

    def test_empty_or_malformed_sources_fail_even_in_skip_mode(self):
        for data in ("", "\n", "{}\n", "[]\n", "not-json\n",
                     json.dumps(session("root", [request(completion=0)]))):
            with self.subTest(data=data[:25]):
                self.path.write_text(data)
                with self.assertRaises(ValueError):
                    read_trace(self.path, "freeinference", invalid_requests="skip")

    def test_scaled_prefix_keeps_selected_lineage_without_session_completion_constraints(self):
        first = request(1000, 100, 3, end_ms=9999999)
        self.write([session("root", [first, request(3000, 200, 2), request(5000, 300, 1)])])
        source = read_trace(self.path, "freeinference")
        workload = scale_trace(source, 3, duration_seconds=.5)
        self.assertEqual(len(workload["requests"]), 2)
        self.assertEqual(len(workload["trace_metadata"]["requests"]), 2)
        self.assertEqual([r["input_tokens"] for r in workload["requests"]], [100, 200])
        self.assertNotIn("end_ms", json.dumps(workload))
        self.assertEqual(source["records"][0]["source_time_ns"], 0)

    def test_cli_strict_rejection_writes_audit_without_workload(self):
        self.write([session("root", [request(), request(completion=0)])])
        out = self.root / "out.json"
        self.assertEqual(self.cli("--output", out), 2)
        self.assertFalse(out.exists())
        audit = json.loads(out.with_suffix(".audit.json").read_text())
        self.assertEqual(audit["status"], "rejected")
        self.assertEqual(audit["rejected_request_count"], 1)

    def test_cli_skip_and_profile_rejection_report_coverage_and_never_clip(self):
        self.write([session("root", [request(prompt=17000), request(completion=0)])])
        out = self.root / "out.json"
        profile = Path(__file__).resolve().parents[2] / "profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json"
        self.assertEqual(self.cli("--invalid-requests", "skip", "--profile", profile, "--output", out), 2)
        self.assertFalse(out.exists())
        audit = json.loads(out.with_suffix(".audit.json").read_text())
        self.assertEqual(audit["status"], "profile_out_of_coverage")
        self.assertEqual(audit["profile_coverage"]["out_of_coverage_count"], 1)
        self.assertEqual(audit["provenance"]["clipped_requests"], 0)

    def test_cli_success_refuses_overwrite_and_preserves_native_input(self):
        self.write([session("root", [request()])])
        out = self.root / "out.json"
        self.assertEqual(self.cli("--output", out), 0)
        self.assertEqual(load_workload(out)[0][0]["input_tokens"], 256)
        self.assertEqual(self.cli("--output", out), 2)
        self.assertEqual(self.cli("--output", self.root / "x.json", "--audit-output", out), 2)



if __name__ == "__main__":
    unittest.main()
