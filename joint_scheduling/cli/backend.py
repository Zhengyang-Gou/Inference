"""Argument registration and handlers for backend commands."""
import json
from pathlib import Path


def register_backend(commands):
    backend = commands.add_parser("backend", help="固定版本网络后端的检查、构建与回归验证")
    backend_commands = backend.add_subparsers(dest="backend_command", required=True)
    backend_commands.add_parser("doctor", help="检查工具链、固定版本和后端状态")
    build = backend_commands.add_parser("build", help="在独立目录中构建后端")
    build.add_argument("--jobs", type=int, default=2)
    smoke = backend_commands.add_parser("smoke", help="执行真实 ns-3 网络与端到端回归")
    smoke.add_argument("--output", type=Path)
    backend.set_defaults(handler=handle_backend)


def handle_backend(args):
    from ..backend.build import doctor, build_backend
    if args.backend_command == "doctor":
        report = doctor()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["backend_ready"] else 2
    if args.backend_command == "build":
        state = build_backend(args.jobs)
        print("后端构建完成：{}".format(state["executable"]))
    else:
        from ..backend.smoke import smoke_test
        print(json.dumps(smoke_test(args.output), ensure_ascii=False, indent=2))
    return 0
