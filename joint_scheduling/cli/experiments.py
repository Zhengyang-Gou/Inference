"""Argument registration and handlers for experiments commands."""
import json
from pathlib import Path


def register_audit_ep(commands):
    audit = commands.add_parser('audit-ep', help='独立核对 EP 路由、激活字节及可选的实际提交依赖')
    audit.add_argument('--output', type=Path, required=True)
    audit.add_argument('--simulate', action='store_true', help='按当前配置完整运行并逐调用核验；主模型请选短程运行的 config.json')
    audit.add_argument('--expert-groups', type=int, help='参考 gate 的专家分组数，与 --max-groups-per-token 一起使用')
    audit.add_argument('--max-groups-per-token', type=int, help='参考 gate 每 token 最多访问几组，仅检查约束，不修改路由')
    audit.set_defaults(handler=handle_audit_ep)


def handle_audit_ep(args):
    from ..config import load_config
    from ..experiments.ep_audit import run_ep_audit
    report = run_ep_audit(load_config(args.config), args.output, args.simulate,
                         expert_groups=args.expert_groups,max_groups_per_token=args.max_groups_per_token)
    print(json.dumps(dict(layout=report['layout']['status'], routing_constraint=report['routing_constraint'],simulation=report['simulation'],
                          output=str(args.output.resolve()/'audit_summary.json')), ensure_ascii=False, indent=2))
    return 0


def register_assess_calibration(commands):
    assess = commands.add_parser("assess-calibration", help="只读取已完成的基线回放，按冻结判据确定承载率区间")
    assess.add_argument("--plan", type=Path, required=True)
    assess.add_argument("--results-root", type=Path, required=True)
    assess.add_argument("--output", type=Path, required=True)
    assess.set_defaults(handler=handle_assess_calibration)


def handle_assess_calibration(args):
    from ..experiments.calibration import assess_calibration
    report = assess_calibration(args.plan, args.results_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps(dict(completed=report["completed_trial_count"], planned=report["trial_count"],
                          selections=report["selections"]), ensure_ascii=False, indent=2))
    return 0


def register_run_experiments(commands):
    experiments = commands.add_parser("run-experiments", help="串行或多进程执行参数矩阵并汇总结果和失败记录")
    experiments.add_argument("--spec", type=Path, required=True)
    experiments.add_argument("--output", type=Path, required=True)
    experiments.add_argument("--plot", action="store_true")
    experiments.add_argument("--jobs", type=int, default=1, help="最多同时运行的独立实验数，默认 1（串行）")
    experiments.add_argument("--fail-fast", action="store_true")
    experiments.add_argument("--dry-run", action="store_true", help="仅显示运行计划，不创建输出或启动网络后端")
    experiments.set_defaults(handler=handle_run_experiments)


def handle_run_experiments(args):
    from ..experiments import plan_suite, run_suite
    from ..experiments.parallel import execution_options
    execution_options(args.jobs, 0)
    if args.dry_run:
        plan = plan_suite(args.spec)
        report={"experiment_type":plan['source_spec'].get('experiment_type','end_to_end'),
                "total_runs": len(plan["runs"]), "runs": plan["runs"],
                "execution": execution_options(args.jobs, len(plan["runs"]), args.fail_fast)}
        if 'comparison_audit' in plan:report['comparison_audit']=plan['comparison_audit']
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    report = run_suite(args.spec, args.output, args.plot, args.fail_fast,
                       progress=lambda row: print("{}: {}{}".format(row["run_id"], row["status"],
                           " — " + row["error"] if row.get("error") else ""), flush=True), jobs=args.jobs)
    print("实验汇总：{}".format(args.output.resolve() / "summary.json"))
    return 0 if report["status"] == "completed" else 2
