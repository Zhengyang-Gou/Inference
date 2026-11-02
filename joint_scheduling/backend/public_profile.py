"""Compose an explicitly estimated 70B profile from public Vidur GPU operators.

This adapter is deliberately limited to matching Llama-3/3.1-70B tensor shapes.
It does not import Vidur's scheduler or predict end-to-end measured performance.
"""

import bisect
import copy
import csv
import hashlib
import math
import statistics
from collections import defaultdict
from pathlib import Path


SHAPE = dict(num_layers=80, hidden_size=8192, intermediate_size=28672,
             num_attention_heads=64, num_key_value_heads=8, head_dim=128, vocab_size=128256)
MLP_OPS = ("input_layernorm", "attn_pre_proj", "attn_rope", "attn_post_proj",
           "post_attention_layernorm", "mlp_up_proj", "mlp_act", "mlp_down_proj", "add")
ATTENTION_OPS = ("attn_input_reshape", "attn_kv_cache_save", "attn_prefill", "attn_decode", "attn_output_reshape")


def _curve(rows, key, operations):
    groups = defaultdict(list)
    for row in rows:
        values = [float(row["time_stats.{}.median".format(op)]) for op in operations]
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("public operator measurements must be finite and nonnegative")
        groups[int(row[key])].append(sum(values))
    if not groups:
        raise ValueError("no matching public operator samples")
    return {tokens: statistics.median(values) for tokens, values in sorted(groups.items())}


def _at(curve, tokens, allow_low_anchor=False):
    keys = sorted(curve)
    if tokens < keys[0] and allow_low_anchor:
        return curve[keys[0]]
    if not keys[0] <= tokens <= keys[-1]:
        raise ValueError("public operator query outside measured range")
    index = bisect.bisect_left(keys, tokens)
    if index < len(keys) and keys[index] == tokens:
        return curve[tokens]
    lo, hi = keys[index - 1], keys[index]
    return curve[lo] + (curve[hi] - curve[lo]) * (tokens - lo) / (hi - lo)


def build_public_profile(mlp_path, attention_path, tp_size=4, source_commit="", prefill_chunk_tokens=0, target_model="meta-llama/Llama-3.1-70B-Instruct"):
    if target_model not in ("meta-llama/Meta-Llama-3-70B", "meta-llama/Llama-3.1-70B-Instruct"):
        raise ValueError("public profile only supports the measured Llama-3-70B or explicit Llama-3.1 proxy")
    if type(tp_size) is not int or tp_size not in (1, 2, 4, 8):
        raise ValueError("70B profile TP must be 1, 2, 4 or 8")
    with Path(mlp_path).open(newline="") as handle:
        mlp = [row for row in csv.DictReader(handle)
               if int(row["num_tensor_parallel_workers"]) == tp_size
               and int(row["n_embd"]) == SHAPE["hidden_size"]
               and int(row["n_head"]) == SHAPE["num_attention_heads"]
               and int(row["n_kv_head"]) == SHAPE["num_key_value_heads"]
               and int(row["n_expanded_embd"]) == SHAPE["intermediate_size"]
               and int(row["vocab_size"]) == SHAPE["vocab_size"] and row["use_gated_mlp"] == "True"]
    with Path(attention_path).open(newline="") as handle:
        attention = [row for row in csv.DictReader(handle)
                     if int(row["num_tensor_parallel_workers"]) == tp_size
                     and int(row["n_embd"]) == SHAPE["hidden_size"]
                     and int(row["n_q_head"]) == SHAPE["num_attention_heads"]
                     and int(row["n_kv_head"]) == SHAPE["num_key_value_heads"]
                     and int(row["block_size"]) == 16
                     and row["attention_backend"] == "AttentionBackend.FLASHINFER"]
    all_attention = attention
    attention = [row for row in all_attention if int(row["batch_size"]) == 1]
    linear = _curve(mlp, "num_tokens", MLP_OPS)
    embedding = _curve(mlp, "num_tokens", ("emb",))
    prefill = _curve([row for row in attention if row["is_prefill"] == "True" and int(row["kv_cache_size"]) == 0],
                     "prefill_chunk_size", ATTENTION_OPS)
    decode = _curve([row for row in attention if row["is_prefill"] == "False" and int(row["prefill_chunk_size"]) == 0],
                    "kv_cache_size", ATTENTION_OPS)
    if min(linear) != 1 or min(embedding) != 1:
        raise ValueError("public linear/embedding samples must include one-token execution")
    pmax, dmax = min(max(linear), max(prefill)), max(decode)
    p_tokens = sorted({1, pmax} | {key for key in linear if key <= pmax} | {key for key in prefill if key <= pmax})
    d_tokens = sorted({1, dmax} | set(decode))
    # All values in Vidur CSVs are milliseconds; ns-3's profile uses seconds.
    p_points = [dict(tokens=tokens,
                     compute_seconds=(_at(linear, tokens) + _at(prefill, tokens, True)) / 1000,
                     collective_bytes=tokens * SHAPE["hidden_size"] * 2) for tokens in p_tokens]
    d_points = [dict(tokens=tokens,
                     compute_seconds=(SHAPE["num_layers"] * (_at(linear, 1) + _at(decode, tokens, True)) + _at(embedding, 1)) / 1000,
                     collective_bytes=SHAPE["hidden_size"] * 2) for tokens in d_tokens]
    batches = [dict(batch_size=1, points=copy.deepcopy(d_points))]
    for batch_size in sorted({int(row["batch_size"]) for row in all_attention}):
        if batch_size <= 1 or not min(linear) <= batch_size <= max(linear):
            continue
        rows = [row for row in all_attention if int(row["batch_size"]) == batch_size
                and row["is_prefill"] == "False" and int(row["prefill_chunk_size"]) == 0]
        if not rows:
            continue
        curve = _curve(rows, "kv_cache_size", ATTENTION_OPS)
        points = [dict(tokens=tokens,
                       compute_seconds=(SHAPE["num_layers"]*(_at(linear,batch_size)+_at(curve,tokens,True))+_at(embedding,batch_size))/1000,
                       collective_bytes=batch_size*SHAPE["hidden_size"]*2)
                  for tokens in sorted({1,max(curve)} | set(curve))]
        batches.append(dict(batch_size=batch_size,points=points))
    layers = [dict(kv_bytes_per_token=2 * 2 * SHAPE["num_key_value_heads"] * SHAPE["head_dim"],
                   collectives_per_step=2, points=copy.deepcopy(p_points)) for _ in range(SHAPE["num_layers"])]
    # Embedding lookup occurs once per forward, rather than once per layer.
    for point in layers[0]["points"]:
        point["compute_seconds"] += _at(embedding, point["tokens"]) / 1000
    hidden, intermediate = SHAPE["hidden_size"], SHAPE["intermediate_size"]
    layer_matrix = 2 * hidden**2 + 2 * hidden * SHAPE["num_key_value_heads"] * SHAPE["head_dim"] + 3 * hidden * intermediate
    matrix_parameters = SHAPE["num_layers"] * layer_matrix + 2 * SHAPE["vocab_size"] * hidden
    norm_parameters = SHAPE["num_layers"] * 2 * hidden + hidden
    # Linear/embedding weights are TP-sharded, RMSNorm weights replicated.
    weights_per_gpu = math.ceil(matrix_parameters * 2 / tp_size) + norm_parameters * 2
    profile = dict(schema_version=2,
                   provenance=dict(synthetic=True,
                       description="Estimated Llama-3.1-70B FP16 proxy composed from shape-matched Llama-3-70B A100 operator medians; not an end-to-end GPU measurement.",
                       target_model="meta-llama/Llama-3.1-70B-Instruct", source_model="meta-llama/Meta-Llama-3-70B",
                       device="A100 80GB", dtype="float16", tensor_parallel_size=tp_size, model_shape=copy.deepcopy(SHAPE),
                       source_repo="https://github.com/microsoft/vidur", source_commit=source_commit,
                       source_files={"mlp_sha256": hashlib.sha256(Path(mlp_path).read_bytes()).hexdigest(),
                                     "attention_sha256": hashlib.sha256(Path(attention_path).read_bytes()).hexdigest()},
                       construction="Sum kernel medians per sample, median across repeated configurations, piecewise linear interpolation, repeat identical transformer layers.",
                       measurement_unit="milliseconds", exported_unit="seconds",
                       prefill_ops=list(MLP_OPS + ATTENTION_OPS), embedding_once_per_forward=True,
                       low_context_assumption="Below the minimum measured attention length, use the minimum-length measured attention cost; linear operators retain their own measured token dependence.",
                       measured_attention_min_prefill_tokens=min(prefill), measured_attention_min_decode_tokens=min(decode),
                       omissions=["final vocabulary projection and final norm timing", "embedding-output collective", "sampling/tokenization/CPU scheduler overhead", "BF16 kernel measurements", "Llama-3.1 RoPE-specific kernel validation"],
                       collectives="Two full-hidden FP16 all-reduces per transformer layer; entirely executed by ns-3, excluded from compute medians.",
                       runtime_scheduling="Runtime selects FIFO/chunked or optional token-budget multi-request Prefill; optional continuous Decode batching with measured batch-specific operators and maximum-context padding.",
                       decode_batch_model="Linear/embedding operators use batch token count; attention uses matching measured batch size and padded maximum context. No communication timing included.",
                       model_config_sources=["https://github.com/microsoft/vidur/blob/" + source_commit + "/vidur/config/model_config.py",
                           "https://github.com/meta-llama/llama-models/blob/main/models/llama3_1/MODEL_CARD.md",
                           "https://huggingface.co/amd/Llama-3.1-70B-Instruct-FP8-KV/blob/main/config.json"],
                       architecture_crosscheck="AMD public configuration used for architecture only; no FP8 timings/weights/KV assumptions adopted."),
                   network_communication_excluded=True,
                   kv_bytes_per_token=sum(layer["kv_bytes_per_token"] for layer in layers),
                   weights_bytes_per_gpu=weights_per_gpu,
                   phases=dict(prefill=dict(tp_size=tp_size, layers=layers),
                               decode=dict(tp_size=tp_size, collectives_per_step=2 * SHAPE["num_layers"], points=d_points, batches=batches)))
    if target_model == "meta-llama/Meta-Llama-3-70B":
        provenance = profile["provenance"]
        provenance.update(target_model=target_model,
            description="Estimated Meta-Llama-3-70B FP16 composed from matching public A100 operator medians; not an end-to-end engine measurement.",
            architecture_crosscheck="Model name and tensor shapes match the public profiling source; no Llama-3.1 transfer assumed.",
            model_config_sources=["https://github.com/microsoft/vidur/blob/"+source_commit+"/vidur/config/model_config.py"])
        provenance["omissions"] = [item for item in provenance["omissions"] if "Llama-3.1" not in item]
    if prefill_chunk_tokens:
        profile["phases"]["prefill"]["chunked"] = build_chunked_points(
            attention, linear, embedding, prefill_chunk_tokens)
        profile["provenance"]["chunked_prefill"] = dict(
            maximum_exported_chunk_tokens=prefill_chunk_tokens,
            timing="Match prefill query chunk and cached context to measured attention; linear/embedding use query tokens only. Uniform layers; embedding once per chunk.",
            multi_request_prefill="Packed linear/embedding at total query tokens; sum independent per-request attention estimates; not a measured multi-request GPU kernel.",
            short_chunk_assumption="Below the minimum chunk with cached-context measurements, anchor attention to that minimum measured query size; linear operators retain query-size dependence.")
    audit = dict(status="estimated_operator_proxy", whole_model_measured=False,
                 measured_linear_range=[min(linear), max(linear)], measured_prefill_attention_range=[min(prefill), max(prefill)],
                 measured_decode_attention_range=[min(decode), max(decode)],
                 exported_prefill_range=[1, pmax], exported_decode_range=[1, dmax],
                 measured_decode_batch_sizes=[b["batch_size"] for b in batches],
                 matching_mlp_rows=len(mlp), matching_attention_rows=len(attention),
                 layers=SHAPE["num_layers"], kv_bytes_per_token=profile["kv_bytes_per_token"],
                 parameter_count=matrix_parameters + norm_parameters, weights_bytes_per_gpu=weights_per_gpu,
                 weights_gib_per_gpu=weights_per_gpu / 2**30,
                 attention_low_length_anchor_is_estimated=True,
                 final_vocabulary_projection_timing_available=False)
    return profile, audit


def build_chunked_points(attention, linear, embedding, maximum):
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("maximum prefill chunk must be a positive integer")
    rows = [r for r in attention if r["is_prefill"] == "True"]
    sizes = sorted({int(r["prefill_chunk_size"]) for r in rows if int(r["kv_cache_size"]) > 0})
    if not sizes:
        raise ValueError("no measured cached-context prefill attention samples")
    if maximum > max(sizes) or maximum > max(linear):
        raise ValueError("maximum prefill chunk exceeds measured coverage")
    curves = {size:_curve([r for r in rows if int(r["prefill_chunk_size"]) == size],
                          "kv_cache_size", ATTENTION_OPS) for size in sizes}
    # Keep measured query sizes and measured small linear sizes. Small attention
    # queries use an explicit conservative anchor rather than zero-context timing.
    queries = sorted({1,maximum} | {s for s in sizes if s <= maximum}
                     | {s for s in linear if s < min(sizes) and s <= maximum})
    groups = []
    for query in queries:
        lo = max((s for s in sizes if s <= query), default=sizes[0])
        hi = min((s for s in sizes if s >= query), default=sizes[-1])
        fraction = 0 if lo == hi else (query-lo)/(hi-lo)
        limit = min(max(curves[lo]), max(curves[hi]))
        contexts = sorted({0,limit} | {k for s in (lo,hi) for k in curves[s] if k <= limit})
        points = []
        for cached in contexts:
            attention_ms = _at(curves[lo],cached)+fraction*(_at(curves[hi],cached)-_at(curves[lo],cached))
            points.append(dict(cached_tokens=cached, compute_seconds=(_at(linear,query)+attention_ms)/1000,
                               embedding_seconds=_at(embedding,query)/1000,attention_seconds=attention_ms/1000,
                               collective_bytes=query*SHAPE["hidden_size"]*2))
        groups.append(dict(chunk_tokens=query,points=points))
    linear_points=[dict(tokens=tokens,compute_seconds=_at(linear,tokens)/1000,
                        embedding_seconds=_at(embedding,tokens)/1000,
                        collective_bytes=tokens*SHAPE["hidden_size"]*2) for tokens in sorted(linear)]
    return dict(collectives_per_layer=2,groups=groups,linear_points=linear_points)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mlp', required=True, type=Path)
    parser.add_argument('--attention', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--tp-size', type=int, default=4)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--prefill-chunk-tokens', type=int, default=0)
    parser.add_argument('--target-model', choices=('meta-llama/Meta-Llama-3-70B','meta-llama/Llama-3.1-70B-Instruct'), default='meta-llama/Llama-3.1-70B-Instruct')
    args = parser.parse_args()
    profile, audit = build_public_profile(args.mlp, args.attention, args.tp_size, args.source_commit, args.prefill_chunk_tokens, args.target_model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(profile, handle, indent=2)
        handle.write('\n')
    print(json.dumps(audit, indent=2))


if __name__ == '__main__':
    main()
