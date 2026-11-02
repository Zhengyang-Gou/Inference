#!/usr/bin/env python3
"""绘制主研究的 160 卡 P/D 分离拓扑示意图。

图中的节点数、P/D 布局和带宽全部读取
joint_scheduling/configs/research/base.json，并在绘制前校验一致性
（GPU 总数、每 Leaf 上下联 1:1、P/D 角色分布），避免图片与配置脱节。

用法（仓库根目录）：
    MPLCONFIGDIR=/tmp/mpl ./scripts/simai-python scripts/plot_research_topology.py \
        --output assets/04_research_topology.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# --------------------------------------------------------------------------
# 配色：与 assets/ 下既有示意图保持一致
# --------------------------------------------------------------------------
BG = "#F7F9FC"
INK = "#1F2D3D"
MUTED = "#54677A"
LINE = "#9DAFC4"

P_FILL, P_EDGE, P_TEXT = "#E8F0FA", "#4A78C2", "#24486E"
D_FILL, D_EDGE, D_TEXT = "#E9F5EE", "#3C9A6E", "#1F5F45"
SPINE_FILL, SPINE_EDGE = "#E8EDF5", "#5F7590"
LEAF_FILL, LEAF_EDGE = "#EDF2FA", "#6C86A8"
KV = "#E08A2E"
ACCENT = "#7A6BC4"

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "joint_scheduling/configs/research/base.json"


def num(value: float) -> str:
    """400.0 -> 400，3.2 -> 3.2。"""
    return f"{value:g}"


# --------------------------------------------------------------------------
# 读取并校验拓扑事实
# --------------------------------------------------------------------------
def load_facts(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    topo = config["topology"]

    leaves = topo["num_leaves"]
    spines = topo["num_spines"]
    nodes_per_leaf = topo["nodes_per_leaf"]
    gpus_per_node = topo["gpus_per_node"]
    nic_gbps = topo["nic_bandwidth_gbps"]
    uplink_gbps = topo["uplink_bandwidth_gbps"]
    intra_gbps = topo["intra_node_bandwidth_gbps"]

    nodes = leaves * nodes_per_leaf
    gpus = nodes * gpus_per_node

    # 每 Leaf 下联 = 节点数 × 单机带宽；上联 = 到每台 Spine 一条链路
    down_gbps = nodes_per_leaf * nic_gbps
    up_gbps = spines * uplink_gbps
    if down_gbps != up_gbps:
        raise SystemExit(
            f"上下联不满足 1:1：下联 {down_gbps} Gbps，上联 {up_gbps} Gbps"
        )

    if nodes_per_leaf != 2:
        raise SystemExit("本图按每 Leaf 2 台机器布局，n=2 之外的拓扑需要改图")
    if gpus_per_node != 8:
        raise SystemExit("本图按每节点 8 GPU 绘制，其他规模需要改图")

    # 从 placements 反推每台机器的角色
    leaf_roles: list[list[str]] = [[] for _ in range(leaves)]
    for placement in topo["placements"]:
        if not placement["gpu_ids"]:
            continue
        node = placement["gpu_ids"][0] // gpus_per_node
        leaf = node // nodes_per_leaf
        role = "P" if placement["role"] == "prefill" else "D"
        while len(leaf_roles[leaf]) <= node % nodes_per_leaf:
            leaf_roles[leaf].append("")
        leaf_roles[leaf][node % nodes_per_leaf] = role

    if any(len(roles) != nodes_per_leaf or "" in roles for roles in leaf_roles):
        raise SystemExit("placements 没有覆盖全部节点，无法确定每台机器的角色")

    p_nodes = sum(roles.count("P") for roles in leaf_roles)
    d_nodes = sum(roles.count("D") for roles in leaf_roles)
    if p_nodes + d_nodes != nodes:
        raise SystemExit("P/D 节点数与节点总数不一致")

    p_gpus, d_gpus = p_nodes * gpus_per_node, d_nodes * gpus_per_node
    if p_gpus + d_gpus != gpus:
        raise SystemExit("P/D GPU 数与 GPU 总数不一致")

    p_leaves = [i for i, roles in enumerate(leaf_roles) if "P" in roles]
    if p_leaves != list(range(len(p_leaves))):
        raise SystemExit("P 节点不在连续的 Leaf 上，EP32 跨度标注需要改图")

    return {
        "leaves": leaves,
        "spines": spines,
        "nodes_per_leaf": nodes_per_leaf,
        "gpus_per_node": gpus_per_node,
        "nodes": nodes,
        "gpus": gpus,
        "per_gpu_gbps": nic_gbps / gpus_per_node,
        "node_tbps": nic_gbps / 1000,
        "uplink_tbps": uplink_gbps / 1000,
        "uplink_total_tbps": up_gbps / 1000,
        "down_total_tbps": down_gbps / 1000,
        "intra_tbps": intra_gbps / 1000,
        "intra_gbps_per_port": intra_gbps,
        "intra_gb_per_s": intra_gbps / 8,
        "link_latency_us": topo["link_latency_us"],
        "intra_latency_us": topo["intra_node_latency_us"],
        "leaf_roles": leaf_roles,
        "p_nodes": p_nodes,
        "d_nodes": d_nodes,
        "p_gpus": p_gpus,
        "d_gpus": d_gpus,
        "p_leaves": (p_leaves[0], p_leaves[-1]),
        "ratio": d_gpus // p_gpus,
    }


# --------------------------------------------------------------------------
# 绘图基元
# --------------------------------------------------------------------------
def rounded(ax, x, y, w, h, fc, ec, lw=1.2, zorder=2, radius=0.05, alpha=1.0):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle=f"round,pad=0,rounding_size={radius}",
            facecolor=fc,
            edgecolor=ec,
            linewidth=lw,
            zorder=zorder,
            alpha=alpha,
        )
    )


def label(ax, x, y, text, size=9, color=INK, weight="normal", ha="center", va="center", **kw):
    return ax.text(
        x, y, text, fontsize=size, color=color, fontweight=weight, ha=ha, va=va, zorder=6, **kw
    )


def hline(ax, x0, x1, y, color=LINE, lw=2.4, zorder=1):
    ax.add_line(Line2D([x0, x1], [y, y], color=color, linewidth=lw, zorder=zorder,
                       solid_capstyle="round"))


def edge(ax, p0, p1, color=LINE, lw=1.3, zorder=1):
    ax.add_line(
        Line2D([p0[0], p1[0]], [p0[1], p1[1]], color=color, linewidth=lw, zorder=zorder,
               solid_capstyle="round")
    )


def arrow(ax, p0, p1, color, lw=1.6, style="<->", rad=0.0, ls="-", zorder=3):
    ax.add_patch(
        FancyArrowPatch(
            p0, p1, arrowstyle=style, mutation_scale=10, color=color, linewidth=lw,
            linestyle=ls, connectionstyle=f"arc3,rad={rad}", zorder=zorder, shrinkA=1, shrinkB=1,
        )
    )


# --------------------------------------------------------------------------
# 全局拓扑面板
# --------------------------------------------------------------------------
def draw_global(ax, f: dict):
    leaves, spines = f["leaves"], f["spines"]
    ax.set_xlim(0, 13.6)
    ax.set_ylim(0, 10)
    ax.axis("off")

    left, right = 0.35, 13.25
    leaf_w, leaf_h, leaf_y = 1.06, 0.6, 6.55
    step = (right - left - leaf_w) / (leaves - 1)
    centers = [left + leaf_w / 2 + i * step for i in range(leaves)]

    node_w, node_h, node_y = 0.5, 0.62, 5.15

    # ---- Spine 层 ----
    spine_w, spine_h, spine_y = 1.7, 0.55, 8.95
    group_w = spines * spine_w + (spines - 1) * 0.2
    spine_left = (left + right + leaf_w) / 2 - group_w / 2
    label(ax, (left + right + leaf_w) / 2, 9.72,
          f"Spine 层：{spines} 台交换机，与 {leaves} 个 Leaf 全互连",
          size=11.5, weight="bold")
    for s in range(spines):
        x = spine_left + s * (spine_w + 0.2)
        rounded(ax, x, spine_y, spine_w, spine_h, SPINE_FILL, SPINE_EDGE, lw=1.1)
        label(ax, x + spine_w / 2, spine_y + spine_h / 2, f"Spine {s}", size=10, color="#3B4C5E")

    # ---- 上联：每 Leaf 到每台 Spine 各一条链路 ----
    uplink_y = 8.28
    hline(ax, centers[0], centers[-1], uplink_y, color="#B4C2D4", lw=4.4)
    for s in range(spines):
        x = spine_left + s * (spine_w + 0.2) + spine_w / 2
        edge(ax, (x, spine_y), (x, uplink_y), color=LINE, lw=1.8)
    for xc in centers:
        edge(ax, (xc, uplink_y), (xc, leaf_y + leaf_h), color=LINE, lw=1.8)

    # ---- Leaf 与节点 ----
    for i, xc in enumerate(centers):
        roles = f["leaf_roles"][i]
        rounded(ax, xc - leaf_w / 2, leaf_y, leaf_w, leaf_h, LEAF_FILL, LEAF_EDGE, lw=1.3)
        label(ax, xc, leaf_y + leaf_h / 2, f"Leaf {i}", size=9.6, color="#33465B")

        node_ids = [i * f["nodes_per_leaf"] + k for k in range(f["nodes_per_leaf"])]
        node_xs = [xc - node_w - 0.03, xc + 0.03]

        edge(ax, (xc, leaf_y), (xc, 6.08), lw=1.3)
        hline(ax, xc - node_w - 0.03, xc + node_w + 0.03, 6.08, lw=1.3)

        for role, nid, nx in zip(roles, node_ids, node_xs):
            fc, ec, tc = (P_FILL, P_EDGE, P_TEXT) if role == "P" else (D_FILL, D_EDGE, D_TEXT)
            rounded(ax, nx, node_y, node_w, node_h, fc, ec, lw=1.4)
            label(ax, nx + node_w / 2, node_y + node_h * 0.66, f"H{nid}", size=8.6, color=tc)
            label(ax, nx + node_w / 2, node_y + node_h * 0.29, role, size=8.4, color=tc,
                  weight="bold")
            edge(ax, (nx + node_w / 2, 6.08), (nx + node_w / 2, node_y + node_h), lw=1.3)

        # 同 Leaf 的 P→D KV 交接：不跨 Spine
        if "P" in roles and "D" in roles:
            arrow(ax, (node_xs[0] + node_w / 2, node_y), (node_xs[1] + node_w / 2, node_y),
                  KV, lw=1.2, rad=0.6, ls=(0, (3, 2)), zorder=4)

    # ---- EP 组跨度 ----
    p_end = centers[f["p_leaves"][1]] + leaf_w / 2
    d_start, d_end = centers[0] - leaf_w / 2, centers[-1] + leaf_w / 2

    arrow(ax, (d_start + 0.2, 4.12), (p_end, 4.12), P_EDGE, lw=1.6)
    label(ax, (d_start + p_end) / 2 + 0.1, 3.8,
          f"P 单元：EP{f['p_gpus']} 跨 Leaf {f['p_leaves'][0]}–{f['p_leaves'][1]}"
          f"（{f['p_nodes']} 节点 / {f['p_gpus']} GPU）",
          size=10, color=P_TEXT)

    arrow(ax, (d_start, 3.12), (d_end, 3.12), D_EDGE, lw=1.6)
    label(ax, (d_start + d_end) / 2, 2.8,
          f"D 单元：EP{f['d_gpus']} 跨全部 {leaves} 个 Leaf（{f['d_nodes']} 节点 / {f['d_gpus']} GPU）",
          size=10, color=D_TEXT)

    label(ax, (d_start + d_end) / 2, 2.2,
          f"P : D = {f['p_gpus']} : {f['d_gpus']} GPU（1 : {f['ratio']} 节点）",
          size=11, color=INK, weight="bold")

    # ---- 图例 ----
    legend_y = 1.6
    items = [
        ("P 节点（Prefill）", P_FILL, P_EDGE),
        ("D 节点（Decode）", D_FILL, D_EDGE),
        ("Leaf 交换机", LEAF_FILL, LEAF_EDGE),
        ("Spine 交换机", SPINE_FILL, SPINE_EDGE),
    ]
    x = 0.6
    for text, fc, ec in items:
        rounded(ax, x, legend_y - 0.12, 0.28, 0.24, fc, ec, lw=1.0, radius=0.03)
        label(ax, x + 0.38, legend_y, text, size=9.2, color=MUTED, ha="left")
        x += 0.38 + len(text) * 0.17 + 0.42
    arrow(ax, (x, legend_y), (x + 0.6, legend_y), KV, lw=1.2, ls=(0, (3, 2)))
    label(ax, x + 0.72, legend_y, "同 Leaf KV 交接", size=9.2, color=MUTED, ha="left")

    # ---- 说明 ----
    notes = [
        f"规模与带宽：{leaves} Leaf × {f['nodes_per_leaf']} 节点 × {f['gpus_per_node']} GPU = "
        f"{f['gpus']} 卡；每 GPU {num(f['per_gpu_gbps'])} Gbps 双向，单机 "
        f"{num(f['node_tbps'])} Tbps，节点内每 GPU {num(f['intra_tbps'])} Tbps。",
        f"上联：每 Leaf 到 {spines} 台 Spine 各 {num(f['uplink_tbps'])} Tbps"
        f"（合计 {num(f['uplink_total_tbps'])} Tbps），与下联 "
        f"{f['nodes_per_leaf']} × {num(f['node_tbps'])} Tbps 保持 1:1；"
        f"远端链路 {num(f['link_latency_us'])} μs。",
        "口径：全部数字都是仿真参数；上联只表示链路容量，不对应真实交换机端口或 rail 布局。",
    ]
    for k, text in enumerate(notes):
        label(ax, 0.35, 1.02 - k * 0.33, text, size=8.8, color=MUTED, ha="left")


# --------------------------------------------------------------------------
# 单个 Leaf 内部面板
# --------------------------------------------------------------------------
def draw_leaf_detail(ax, f: dict):
    ax.set_xlim(0, 6)
    ax.set_ylim(0, 10)
    ax.axis("off")

    label(ax, 3.0, 9.55,
          f"1 个 P 节点 + 1 个 D 节点，各 {f['gpus_per_node']} 张 GPU", size=9.6, color=MUTED)
    label(ax, 3.0, 9.12,
          f"每个节点 {f['gpus_per_node']} × {num(f['per_gpu_gbps'])} Gbps 全双工接入",
          size=9.2, color=MUTED)

    # Leaf 交换机
    rounded(ax, 1.2, 8.45, 3.6, 0.55, LEAF_FILL, LEAF_EDGE, lw=1.3)
    label(ax, 3.0, 8.72, "Leaf 0 交换机", size=9.8, color="#33465B")

    # 两个节点并排，各自 2×4 个 GPU
    nodes = (
        ("P 节点 H0", 0.22, P_FILL, P_EDGE, P_TEXT, 0),
        ("D 节点 H1", 3.22, D_FILL, D_EDGE, D_TEXT, f["gpus_per_node"]),
    )
    cw, cy, ch = 2.56, 5.55, 1.92
    for title, x, fc, ec, tc, offset in nodes:
        rounded(ax, x, cy, cw, ch, fc, ec, lw=1.4, radius=0.07)
        label(ax, x + 0.18, cy + ch - 0.28, title, size=9.4, color=tc, ha="left", weight="bold")
        label(ax, x + cw - 0.18, cy + ch - 0.28,
              f"GPU {offset}–{offset + f['gpus_per_node'] - 1}", size=8.4, color=tc, ha="right")
        gw, gh, gap = 0.5, 0.52, 0.105
        gx0, gy = x + 0.19, cy + 0.62
        for g in range(f["gpus_per_node"]):
            gx = gx0 + (g % 4) * (gw + gap)
            gyy = gy if g < 4 else gy - gh - 0.14
            rounded(ax, gx, gyy, gw, gh, "white", ec, lw=1.0, radius=0.03, zorder=3)
            label(ax, gx + gw / 2, gyy + gh / 2, f"G{offset + g}", size=7.2, color=tc)
        # 交换机到该节点
        cx = x + cw / 2
        edge(ax, (cx, 8.45), (cx, cy + ch), lw=1.8)

    # P → D 的 KV 交接：同 Leaf 内直接走本 Leaf 交换机
    arrow(ax, (0.9, cy), (5.1, cy), KV, lw=1.6, rad=0.45, ls=(0, (3.5, 2.5)))
    label(ax, 3.0, 4.42, "同 Leaf KV 交接（不跨 Spine）", size=8.8, color=KV)

    rows = [
        ("机内", f"每 GPU 独立发送/接收端口各 {num(f['intra_tbps'])} Tbps"
                 f"（{num(f['intra_gb_per_s'])} GB/s），{num(f['intra_latency_us'])} μs，可并发。"),
        ("机间", f"每 GPU 一个 {num(f['per_gpu_gbps'])} Gbps 网卡端点，单机 "
                 f"{num(f['node_tbps'])} Tbps，{f['leaves']} 个 Leaf 经 {f['spines']} 个 Spine 互通。"),
        ("竞争", "跨 Leaf 的 EP dispatch/combine 与 P→D 的 KV 走同一张网，这是争用来源。"),
    ]
    for k, (tag, text) in enumerate(rows):
        y = 2.62 - k * 0.72
        label(ax, 0.22, y, tag, size=9.2, color=ACCENT, ha="left", weight="bold")
        label(ax, 0.78, y, text, size=8.2, color=MUTED, ha="left")


def main() -> None:
    parser = argparse.ArgumentParser(description="绘制 160 卡 P/D 分离拓扑示意图")
    parser.add_argument("--output", type=Path, default=Path("assets/04_research_topology.png"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()

    facts = load_facts(args.config)

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Noto Sans CJK SC", "Noto Sans CJK JP", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "figure.facecolor": BG,
            "savefig.facecolor": BG,
        }
    )

    fig = plt.figure(figsize=(16.5, 9.6))
    ax_main = fig.add_axes([0.005, 0.02, 0.685, 0.86])
    ax_detail = fig.add_axes([0.695, 0.04, 0.30, 0.82])

    p_first, p_last = facts["p_leaves"]
    d_first, d_last = p_last + 1, facts["leaves"] - 1
    d_range = f"Leaf {d_first}" if d_first == d_last else f"Leaf {d_first}–{d_last}"
    fig.text(0.012, 0.958, f"P/D 分离的 MoE 推理仿真拓扑：{facts['gpus']} 张 H800",
             fontsize=20, color=INK, fontweight="bold", ha="left", va="center")
    fig.text(0.012, 0.912,
             f"{facts['nodes']} 台机器分在 {facts['leaves']} 个 Leaf 下："
             f"Leaf {p_first}–{p_last} 各 1 个 Prefill + 1 个 Decode，"
             f"{d_range} 各 2 个 Decode",
             fontsize=11.5, color=MUTED, ha="left", va="center")

    draw_global(ax_main, facts)
    draw_leaf_detail(ax_detail, facts)

    fig.add_artist(Line2D([0.687, 0.687], [0.05, 0.88], color="#D5DEE9", linewidth=1.0))
    fig.text(0.695, 0.885, "单 Leaf 放大（Leaf 0）", fontsize=11.5, color=INK,
             fontweight="bold", ha="left")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=args.dpi, bbox_inches="tight", pad_inches=0.22)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
