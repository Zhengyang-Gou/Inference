"""Argument registration and handlers for workload commands."""
import json
from pathlib import Path


def register_generate_workload(commands):
    generate = commands.add_parser("generate-workload", help="生成可重复的请求到达、长度和 SLO 负载")
    generate.add_argument("--spec", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--seed", type=int, help="覆盖负载规格中的随机种子")
    generate.set_defaults(handler=handle_generate_workload)


def handle_generate_workload(args):
    from ..validation import read_json
    from ..workload import write_workload
    workload = write_workload(read_json(args.spec), args.output, args.seed)
    print("已生成 {} 个请求：{}".format(len(workload["requests"]), args.output.resolve()))
    return 0


def register_import_trace(commands):
    trace = commands.add_parser("import-trace", help="导入真实来源请求轨迹，保留长度配对并可按到达率缩放时间")
    trace.add_argument("--trace", type=Path, required=True)
    trace.add_argument("--format", choices=("azure", "mooncake", "freeinference"), required=True)
    trace.add_argument("--source-url", default="")
    trace.add_argument("--source-synthetic", action="store_true", help="原始文件本身是合成轨迹时必须声明")
    trace.add_argument("--sha256", help="验证已冻结的原始文件")
    trace.add_argument("--invalid-requests", choices=("error", "skip"), default="error",
                       help="FreeInference 无效请求的处理；默认拒绝，skip 明确记录排除项")
    trace.add_argument("--audit-output", type=Path,
                       help="导入审计输出；FreeInference 默认写入同名 .audit.json，质量拒绝和曲线越界时也写出")
    trace.add_argument("--target-rps", type=float)
    trace.add_argument("--duration-seconds", type=float, help="显式选取缩放后时间前缀，不重复或截短请求")
    trace.add_argument("--request-prefix", default="trace")
    trace.add_argument("--ttft-slo-seconds", type=float)
    trace.add_argument("--itl-slo-seconds", type=float)
    trace.add_argument("--profile", type=Path, help="检查所有请求及最终 Decode 上下文；越界拒绝写出")
    trace.add_argument("--output", type=Path, required=True)
    trace.set_defaults(handler=handle_import_trace)


def handle_import_trace(args):
    from ..backend.compute_profile import ComputeProfile
    from ..workload.freeinference import TraceImportError
    from ..workload.trace import read_trace, scale_trace, profile_coverage
    audit_path = args.audit_output
    if audit_path is None and args.format == "freeinference":
        audit_path = args.output.with_suffix(".audit.json")
    if audit_path is not None and audit_path.resolve() == args.output.resolve():
        raise ValueError("workload and import audit must use different output paths")
    for path in (args.output, audit_path):
        if path is not None and path.exists():
            raise ValueError("refusing to overwrite {}".format(path))
    try:
        source = read_trace(args.trace, args.format, args.source_url, args.sha256, args.source_synthetic,
                            invalid_requests=args.invalid_requests)
    except TraceImportError as error:
        if audit_path is not None:
            _write_import_json(audit_path, dict(error.audit, status="rejected", error=str(error)))
        raise
    try:
        workload = scale_trace(source, args.target_rps, request_prefix=args.request_prefix,
                               duration_seconds=args.duration_seconds,
                               ttft_slo_seconds=args.ttft_slo_seconds, itl_slo_seconds=args.itl_slo_seconds)
        coverage = profile_coverage(workload, ComputeProfile(args.profile)) if args.profile else None
    except (ValueError, OSError) as error:
        if audit_path is not None:
            _write_import_json(audit_path, dict(source.get("audit", {}), status="rejected", error=str(error)))
        raise
    if audit_path is not None:
        _write_import_json(audit_path, dict(source.get("audit", {}),
                           status="profile_out_of_coverage" if coverage and coverage["out_of_coverage_count"] else "accepted",
                           selected_request_count=len(workload["requests"]),
                           provenance=workload["provenance"], profile_coverage=coverage))
    if coverage is not None and coverage["out_of_coverage_count"]:
        raise ValueError("{} requests exceed the profile; no token clipping performed".format(coverage["out_of_coverage_count"]))
    _write_import_json(args.output, workload)
    print(json.dumps(dict(output=str(args.output.resolve()), requests=len(workload["requests"]),
                          provenance=workload["provenance"], coverage=coverage,
                          audit_output=str(audit_path.resolve()) if audit_path else None), ensure_ascii=False, indent=2))
    return 0


def _write_import_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
