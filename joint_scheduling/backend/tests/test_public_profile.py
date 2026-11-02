import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from joint_scheduling.backend.public_profile import build_public_profile, MLP_OPS, ATTENTION_OPS
from joint_scheduling.backend.runtime import ComputeProfile


class PublicProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mlp = self.root / "mlp.csv"
        self.attn = self.root / "attention.csv"
        rows = []
        for tokens in (1,32,64):
            row = dict(num_tensor_parallel_workers=4,n_embd=8192,n_head=64,n_kv_head=8,
                       n_expanded_embd=28672,vocab_size=128256,use_gated_mlp="True",num_tokens=tokens)
            row.update({"time_stats.{}.median".format(op):1.0 for op in MLP_OPS+("emb",)})
            rows.append(row)
        wrong_tp = copy.deepcopy(rows[0])
        wrong_tp["num_tensor_parallel_workers"] = 8
        wrong_tp["time_stats.mlp_up_proj.median"] = 99999
        rows.append(wrong_tp)
        self.write(self.mlp, rows)
        rows = []
        for prefill in (True,False):
            for tokens in (32,64):
                row = dict(num_tensor_parallel_workers=4,n_embd=8192,n_q_head=64,n_kv_head=8,batch_size=1,
                           block_size=16,attention_backend="AttentionBackend.FLASHINFER",is_prefill=str(prefill),
                           prefill_chunk_size=tokens if prefill else 0,kv_cache_size=0 if prefill else tokens)
                row.update({"time_stats.{}.median".format(op):0.0 for op in ATTENTION_OPS})
                row["time_stats.attn_kv_cache_save.median"] = 1.0
                row["time_stats.attn_prefill.median"] = 3.0 if prefill else 0.0
                row["time_stats.attn_decode.median"] = 0.0 if prefill else 2.0
                rows.append(row)
        self.write(self.attn, rows)

    @staticmethod
    def write(path, rows):
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    def test_operator_units_layers_and_communication_are_independent(self):
        raw, audit = build_public_profile(self.mlp,self.attn,4,"test-commit")
        path = self.root / "profile.json"
        path.write_text(json.dumps(raw))
        profile = ComputeProfile(path)
        # 80*(9 MLP + 4 attention) ms, plus one 1ms embedding.
        self.assertAlmostEqual(sum(r["compute_seconds"] for r in profile.prefill_steps(64,4)),1.041)
        self.assertAlmostEqual(profile.lookup("decode",64,4)["compute_seconds"],.961)
        self.assertEqual(len(profile.prefill_steps(64,4)),80)
        self.assertEqual(sum(r["collectives_per_step"] for r in profile.prefill_steps(64,4)),160)
        self.assertEqual(profile.lookup("decode",64,4)["collectives_per_step"],160)
        self.assertEqual(profile.kv_bytes_per_token,327680)
        self.assertEqual(audit["parameter_count"],70553706496)
        self.assertTrue(raw["network_communication_excluded"])
        self.assertTrue(raw["provenance"]["synthetic"])
        self.assertEqual(audit["status"],"estimated_operator_proxy")
        with self.assertRaises(ValueError):
            profile.lookup("decode",65,4)

    def test_short_attention_anchor_is_explicit_and_layers_are_distinct_objects(self):
        raw, audit = build_public_profile(self.mlp,self.attn)
        self.assertEqual(audit["measured_prefill_attention_range"],[32,64])
        self.assertTrue(audit["attention_low_length_anchor_is_estimated"])
        raw["phases"]["prefill"]["layers"][0]["points"][0]["compute_seconds"] = 99
        self.assertNotEqual(raw["phases"]["prefill"]["layers"][1]["points"][0]["compute_seconds"],99)

    def test_batch_cost_uses_matching_operators_and_scales_collective_payload(self):
        with self.attn.open(newline="") as handle:
            rows=list(csv.DictReader(handle))
        extra=[]
        for row in rows:
            if row["is_prefill"] == "False":
                row=copy.deepcopy(row); row["batch_size"]=4
                row["time_stats.attn_decode.median"]=4.0
                extra.append(row)
        self.write(self.attn,rows+extra)
        raw,audit=build_public_profile(self.mlp,self.attn)
        path=self.root/"batch.json";path.write_text(json.dumps(raw));p=ComputeProfile(path)
        self.assertEqual(audit["measured_decode_batch_sizes"],[1,4])
        sample=p.lookup_decode_batch([64]*4,4)
        self.assertAlmostEqual(sample["compute_seconds"],1.121)
        self.assertEqual(sample["collective_bytes"],4*8192*2)
        self.assertEqual(sample["collectives_per_step"],160)
        self.assertLess(sample["compute_seconds"],4*p.lookup("decode",64,4)["compute_seconds"])

    def test_chunk_timing_uses_cached_context_and_query_only_linear_work(self):
        with self.attn.open(newline="") as handle:
            rows=list(csv.DictReader(handle))
        extras=[]
        for row in rows:
            if row["is_prefill"] == "True":
                row=copy.deepcopy(row);row["kv_cache_size"]=64
                row["time_stats.attn_prefill.median"]=7.0
                extras.append(row)
        self.write(self.attn,rows+extras)
        raw,audit=build_public_profile(self.mlp,self.attn,prefill_chunk_tokens=32)
        path=self.root/"chunk.json";path.write_text(json.dumps(raw));p=ComputeProfile(path)
        chunks=p.prefill_chunks(64,4,32)
        # Second chunk attends to 32 existing tokens: 6ms vs 4ms attention.
        self.assertAlmostEqual(chunks[0]["steps"][1]["compute_seconds"],.013)
        self.assertAlmostEqual(chunks[1]["steps"][1]["compute_seconds"],.015)
        self.assertEqual(chunks[1]["steps"][0]["collective_bytes"],32*8192*2)
        self.assertEqual(len(chunks),2)
        self.assertIn("short_chunk_assumption",raw["provenance"]["chunked_prefill"])
        packed=p.prefill_batch_steps([dict(chunk_tokens=16,cached_tokens=0),dict(chunk_tokens=16,cached_tokens=32)],4)
        self.assertAlmostEqual(sum(step["compute_seconds"] for step in packed),1.521)
        self.assertEqual(packed[0]["collective_bytes"],32*8192*2)
        self.assertEqual(sum(s["collectives_per_step"] for s in packed),160)

    def test_matching_source_model_is_named_without_changing_measurements(self):
        legacy,_=build_public_profile(self.mlp,self.attn)
        matched,_=build_public_profile(self.mlp,self.attn,target_model="meta-llama/Meta-Llama-3-70B")
        self.assertEqual(matched["phases"],legacy["phases"])
        self.assertEqual(matched["provenance"]["source_model"],matched["provenance"]["target_model"])
        self.assertFalse(any("Llama-3.1" in item for item in matched["provenance"]["omissions"]))
        self.assertTrue(matched["provenance"]["synthetic"])
        with self.assertRaises(ValueError):build_public_profile(self.mlp,self.attn,target_model="unsupported")

    def test_missing_matching_measurements_rejected(self):
        with self.assertRaisesRegex(ValueError,"no matching"):
            build_public_profile(self.mlp,self.attn,2)


if __name__ == "__main__":
    unittest.main()
