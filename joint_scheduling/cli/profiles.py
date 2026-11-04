"""Argument registration and handlers for profiles commands."""
import json
from pathlib import Path


def register_build_public_profile(commands):
    public = commands.add_parser("build-public-profile", help="从公开 Vidur A100 算子 CSV 构建明确标记为估算的 70B 逐层参数")
    public.add_argument("--mlp", type=Path, required=True)
    public.add_argument("--attention", type=Path, required=True)
    public.add_argument("--source-commit", default="")
    public.add_argument("--tp", type=int, default=4)
    public.add_argument("--prefill-chunk-tokens", type=int, default=0)
    public.add_argument("--target-model", choices=("meta-llama/Meta-Llama-3-70B", "meta-llama/Llama-3.1-70B-Instruct"), default="meta-llama/Llama-3.1-70B-Instruct")
    public.add_argument("--output", type=Path, required=True)
    public.set_defaults(handler=handle_build_public_profile)


def handle_build_public_profile(args):
    from ..backend.public_profile import build_public_profile
    profile, audit = build_public_profile(args.mlp, args.attention, args.tp, args.source_commit, args.prefill_chunk_tokens, args.target_model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(profile, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps(dict(profile=str(args.output.resolve()), audit=audit), ensure_ascii=False, indent=2))
    return 0


def register_build_moe_profile(commands):
    moe = commands.add_parser("build-moe-profile", help="从 H800 DeepSeek 公开离线数据或 AIOB 文件构建 MoE 仿真代价 profile")
    moe.add_argument("--model-config", type=Path, required=True)
    moe.add_argument("--prefill-trace", type=Path)
    moe.add_argument("--decode-trace", type=Path)
    moe.add_argument("--aiob-manifest", type=Path)
    moe.add_argument("--tp", type=int, default=4)
    moe.add_argument("--ep", type=int, default=8)
    moe.add_argument("--prefill-ep", type=int, help="Prefill EP size; overrides --ep for this phase")
    moe.add_argument("--decode-ep", type=int, help="Decode EP size; overrides --ep for this phase")
    moe.add_argument("--max-context", type=int, default=16384)
    moe.add_argument("--prefill-chunk-tokens", type=int, default=512)
    moe.add_argument("--prefill-batch-token-budget", type=int, default=2048)
    moe.add_argument("--decode-batch-size", type=int, default=64)
    moe.add_argument("--output", type=Path, required=True)
    moe.set_defaults(handler=handle_build_moe_profile)


def handle_build_moe_profile(args):
    from ..backend.moe_profile import extract_public_anchors, load_aiob_anchors, build_moe_profile
    from ..backend.compute_profile import ComputeProfile
    if bool(args.aiob_manifest) == bool(args.prefill_trace or args.decode_trace) or (not args.aiob_manifest and not (args.prefill_trace and args.decode_trace)):
        raise ValueError("provide both --prefill-trace/--decode-trace, or one --aiob-manifest")
    anchors = (load_aiob_anchors(args.aiob_manifest) if args.aiob_manifest else
               extract_public_anchors(args.prefill_trace,args.decode_trace))
    profile,audit = build_moe_profile(args.model_config,anchors,args.tp,args.ep,args.max_context,
        args.prefill_chunk_tokens,args.prefill_batch_token_budget,args.decode_batch_size,
        prefill_ep_size=args.prefill_ep,decode_ep_size=args.decode_ep)
    audit_path = args.output.with_suffix(".audit.json")
    if args.output.exists() or audit_path.exists():
        raise ValueError("profile or audit output already exists")
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        candidate=Path(folder)/"profile.json"
        candidate.write_text(json.dumps(profile,allow_nan=False))
        checked=ComputeProfile(candidate)
        checked.validate_prefill_batch_coverage(args.max_context,args.prefill_chunk_tokens,args.prefill_batch_token_budget,args.tp)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("x",encoding="utf-8") as handle:
        json.dump(profile,handle,ensure_ascii=False,indent=2,allow_nan=False);handle.write("\n")
    with audit_path.open("x",encoding="utf-8") as handle:
        json.dump(audit,handle,ensure_ascii=False,indent=2,allow_nan=False);handle.write("\n")
    print(json.dumps(dict(profile=str(args.output.resolve()),audit=str(audit_path.resolve()),
        status=audit['status'],coverage=audit['exported_coverage'],weights_gib_per_gpu=audit['weights_gib_per_gpu']),ensure_ascii=False,indent=2))
    return 0
