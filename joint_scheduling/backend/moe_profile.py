"""DeepSeek-V3 profile construction from attributable GPU compute anchors.

The exported grid is an *estimate*, including when its anchors are measured.
No missing AIOB section is replaced with SimAI's default computation time.
CUDA communication/wait durations never enter the compute-only anchors.
"""
import copy
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

from ..validation import number, read_json


PUBLIC_REVISION = "449602428a1b023acb8a505d4f34fef536535db6"
PUBLIC_SHA256 = {
    "prefill": "1078dd96ba9d28e23ca0a5e50107024850cc7a9722ac4b0a4ca35d960738dc79",
    "decode": "6dd02bfb01ac2e0881f77003f8d8b20420c48d769df863e709623654e9ebfb62",
}
ROLES = ("attention_projection", "attention_core", "dense_mlp", "shared_mlp",
         "router", "norm", "expert")
UNITS = {"seconds": 1., "milliseconds": .001, "microseconds": .000001}


def file_identity(path):
    path = Path(path)
    return dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _kernel_part(name):
    match = re.search(r"fp8_gemm_kernel<(\d+)u,\s*(\d+)u,.*GemmType\)(\d+)", name)
    if match:
        n, k, kind = map(int, match.groups())
        if (n, k) in ((4096,7168), (7168,2048)):
            return ("expert" if kind else "shared_mlp"), ("up" if n == 4096 else "down")
        if (n, k) in ((36864,7168), (7168,18432)) and kind == 0:
            return "dense_mlp", ("up" if n == 36864 else "down")
        if (n, k) in ((2112,7168), (24576,1536), (7168,16384)) and kind == 0:
            return "attention_projection", None
    if "flash::compute_attn_ws<" in name or "flash::flash_fwd_splitkv_mla_" in name:
        return "attention_core", None
    if ("fp8_ptp128c_outfmt_head::GemmKernel" in name or
            "bf16_128x128_64x3_nn_align8" in name or "bf16_128x128_64x3_tn_align8" in name or
            "rotary_embedding_with_kv_cache_kernel" in name):
        return "attention_projection", None
    if "top2_sum_gate<" in name or "tilesize128x256x64_warpgroupsize2x1x1" in name or "bf16_128x64_64x4_tn_align8" in name:
        return "router", None
    if name == "_layer_norm_kernel":
        return "norm", None
    return None, None


def _component(name,part):
    """Group alternate launch configurations of the same operation together."""
    match=re.search(r"fp8_gemm_kernel<(\d+)u,\s*(\d+)u",name)
    if match:return 'gemm:'+':'.join(match.groups())
    if 'swiglu_forward' in name:return 'activation'
    if 'flash_fwd_splitkv_mla_combine_kernel' in name:return 'attention_combine'
    if 'flash::' in name:return 'attention'
    if 'rotary_embedding' in name:return 'rope_and_kv_save'
    if 'fp8_ptp128c_outfmt_head' in name:return 'kv_expansion'
    if 'bf16_128x128_64x3_nn' in name:return 'q_bmm'
    if 'bf16_128x128_64x3_tn' in name:return 'v_bmm'
    if 'top2_sum_gate' in name:return 'topk_gate'
    if 'tilesize128x256' in name or 'bf16_128x64_64x4_tn' in name:return 'router_projection'
    return name


def extract_public_anchors(prefill_path, decode_path):
    """Read the pinned official H800 traces, retaining selected kernel evidence.

    The README fixes the per-GPU batch and two micro-batches. These are *not*
    inferred from CPU annotations or the much longer captured wall-clock span.
    """
    sources, observations, evidence = [], {}, {}
    for phase, path in (("prefill",prefill_path), ("decode",decode_path)):
        identity = file_identity(path)
        if identity["sha256"] != PUBLIC_SHA256[phase]:
            raise ValueError("{} is not the pinned official DeepSeek {} trace".format(path, phase))
        trace = read_json(path)
        expected_ep = 32 if phase == "prefill" else 128
        if trace.get("distributedInfo",{}).get("world_size") != expected_ep or not any(
                d.get("name") == "NVIDIA H800" for d in trace.get("deviceProperties",[])):
            raise ValueError("public trace device/EP metadata disagrees with the pinned source")
        kernels = sorted((e for e in trace["traceEvents"] if e.get("cat") == "kernel" and e.get("ph") == "X"), key=lambda e:e["ts"])
        groups, components, pending = defaultdict(list), defaultdict(list), {}
        excluded_communication = excluded_other = 0
        for event in kernels:
            name = event["name"]
            duration = number(event["dur"], "CUDA kernel duration", positive=True) * 1e-6
            if "::ep::" in name or "nccl" in name.lower():
                excluded_communication += 1
                continue
            role, part = _kernel_part(name)
            stream = event.get("args",{}).get("stream", event.get("tid"))
            if role and part == "up":
                pending[stream] = role
            if "swiglu_forward" in name and stream in pending:
                role = pending[stream]
            if role:
                groups[(role,name)].append(duration)
                components[(role,_component(name,part))].append(duration)
            else:
                excluded_other += 1
            if part == "down":
                pending.pop(stream,None)
        roles = {role:sum(statistics.median(v) for (r,_),v in components.items() if r == role) for role in ROLES}
        # Two transformer input norms; the latent Q/K normalization is included
        # as a third norm proxy. This is explicitly an approximate composition.
        roles["norm"] *= 3
        if any(roles[r] <= 0 for r in ROLES):
            raise ValueError("public trace lacks one of the required compute roles")
        observations[phase] = dict(tp_size=1, expert_timing_tp=1, ep_size=expected_ep,
            tokens=8192 if phase == "prefill" else 64, context_tokens=4096,
            expert_assignments=65536 if phase == "prefill" else 512,
            active_experts=8 if phase == "prefill" else 2, roles_seconds=roles)
        identity.update(source_url="https://raw.githubusercontent.com/deepseek-ai/profile-data/"+PUBLIC_REVISION+"/"+phase+".json")
        sources.append(identity)
        evidence[phase] = dict(selected=[dict(role=r,kernel=n,count=len(v),median_seconds=statistics.median(v))
            for (r,n),v in groups.items()], excluded_communication_kernels=excluded_communication,
            excluded_other_kernels=excluded_other, source_trace_name=trace.get("traceName"),
            components=[dict(role=r,component=n,count=len(v),median_seconds=statistics.median(v)) for (r,n),v in components.items()],
            duration_unit="microseconds", aggregation="Median per semantic compute component across alternate launch configurations, then sum component medians; not elapsed wall time. Norm component multiplied by three.")
    return dict(schema_version=1, model="deepseek-ai/DeepSeek-V3", gpu="NVIDIA H800",
        source_kind="official_H800_CUDA_kernel_anchors", sources=sources, observations=observations,
        extraction=evidence, source_revision=PUBLIC_REVISION)


def load_aiob_anchors(manifest_path):
    """Import caller-attributed AIOB sections with explicit units and shapes.

    Unit declarations are mandatory: upstream versions mix time conventions.
    This path never imports PyTorch or silently substitutes missing stages.
    """
    manifest_path = Path(manifest_path)
    spec = read_json(manifest_path)
    if spec.get("schema_version") != 1 or spec.get("model") != "deepseek-ai/DeepSeek-V3" or spec.get("gpu") != "NVIDIA H800":
        raise ValueError("AIOB manifest must identify DeepSeek-V3 and NVIDIA H800")
    observations, sources = {}, [file_identity(manifest_path)]
    samples = spec.get("samples")
    if not isinstance(samples,list) or len(samples) != 2:
        raise ValueError("AIOB anchor manifest needs one prefill and one decode sample")
    for sample in samples:
        required={"phase","path","time_unit","roles","tp_size","expert_timing_tp","ep_size","tokens",
                  "context_tokens","expert_assignments","active_experts"}
        if not isinstance(sample,dict) or not required <= set(sample):
            raise ValueError("AIOB sample must explicitly identify its phase, units, role mapping and measurement coordinates")
        phase = sample.get("phase")
        if phase not in ("prefill","decode") or phase in observations:
            raise ValueError("AIOB sample phases must be unique prefill/decode")
        path = manifest_path.parent / sample["path"]
        contents=path.read_text()
        header=re.search(r"^inference_term:\s*(\w+)\s*$",contents,re.MULTILINE)
        if header is None or header[1] != phase:
            raise ValueError('AIOB inference phase header disagrees with manifest')
        sections, current = {}, None
        for line in contents.splitlines():
            header = re.fullmatch(r"([A-Za-z_][\w]*):\s*",line)
            if header:
                current = header[1]
            match = re.fullmatch(r"\s+time_gpu_avg:\s*(\S+)\s*",line)
            if match:
                if current is None or current in sections:
                    raise ValueError("AIOB contains unattributed or duplicate average timing")
                sections[current] = number(float(match[1]),"AIOB average",positive=True)
        if sample.get("time_unit") not in UNITS:
            raise ValueError("AIOB time_unit must be explicit seconds/milliseconds/microseconds")
        roles = sample.get("roles")
        if not isinstance(roles,dict) or set(roles) != set(ROLES):
            raise ValueError("AIOB roles must explicitly map every compute role")
        costs = {}
        for role, names in roles.items():
            if not isinstance(names,list) or not names or len(set(names)) != len(names):
                raise ValueError("AIOB role requires distinct section names")
            value = 0.
            for name in names:
                if name not in sections:
                    raise ValueError("missing AIOB section {}; no default compute time allowed".format(name))
                unit = sample.get("section_units",{}).get(name,sample["time_unit"])
                if unit not in UNITS:
                    raise ValueError("unknown AIOB section time unit")
                value += sections[name] * UNITS[unit]
            costs[role] = value
        observation = {k:sample[k] for k in ("tp_size","expert_timing_tp","ep_size","tokens","context_tokens","expert_assignments","active_experts")}
        observation["roles_seconds"] = costs
        for key in required-{'phase','path','time_unit','roles'}:
            number(observation[key],'AIOB '+key,positive=True,integral=True)
        observations[phase] = observation
        sources.append(dict(file_identity(path), phase=phase, role_sections=copy.deepcopy(roles),
                            time_unit=sample["time_unit"],section_units=sample.get("section_units",{})))
    return dict(schema_version=1,model=spec["model"],gpu=spec["gpu"],source_kind="AICB_AIOB_explicit_section_anchors",
                sources=sources,observations=observations)


def _knots(maximum, zero=False):
    values={0} if zero else {1}
    n=1
    while n < maximum:
        values.add(n); n*=2
    values.add(maximum)
    return sorted(values)


def build_moe_profile(model_config_path, anchors, tp_size=4, ep_size=8,
                      max_context=16384, prefill_chunk_tokens=512,
                      prefill_batch_token_budget=2048, decode_batch_size=64, *,
                      prefill_ep_size=None, decode_ep_size=None):
    """Export an offline anchor-derived simulation cost model."""
    for key,value in dict(tp_size=tp_size,ep_size=ep_size,max_context=max_context,
        prefill_chunk_tokens=prefill_chunk_tokens,prefill_batch_token_budget=prefill_batch_token_budget,
        decode_batch_size=decode_batch_size).items():
        number(value,key,positive=True,integral=True)
    phase_eps = dict(prefill=ep_size if prefill_ep_size is None else prefill_ep_size,
                     decode=ep_size if decode_ep_size is None else decode_ep_size)
    for phase, size in phase_eps.items():
        number(size, phase+'_ep_size', positive=True, integral=True)
        if 256 % size:
            raise ValueError('DeepSeek experts must divide each phase EP evenly')
    phase_layout = prefill_ep_size is not None or decode_ep_size is not None
    if prefill_chunk_tokens > max_context or prefill_batch_token_budget < prefill_chunk_tokens:
        raise ValueError("profile chunk must fit the context and packed token budget")
    if anchors.get("gpu") != "NVIDIA H800" or anchors.get("model") != "deepseek-ai/DeepSeek-V3":
        raise ValueError("this builder only supports attributable H800 DeepSeek-V3 anchors")
    shape = read_json(model_config_path)
    expected = dict(model_type="deepseek_v3",hidden_size=7168,num_hidden_layers=61,first_k_dense_replace=3,
        num_routed_experts=256,num_experts_per_tok=8,moe_intermediate_size=2048,intermediate_size=18432,
        kv_lora_rank=512,q_lora_rank=1536,qk_nope_head_dim=128,qk_rope_head_dim=64,
        v_head_dim=128,num_attention_heads=128,num_shared_experts=1,vocab_size=129280,n_group=8,topk_group=4)
    if any(shape.get(k) != v for k,v in expected.items()):
        raise ValueError("model configuration does not match supported DeepSeek-V3 shapes")
    from .expert_routing import balanced_group_pattern
    routing_pattern = balanced_group_pattern(shape['num_routed_experts'],shape['num_experts_per_tok'],
                                            shape['n_group'],shape['topk_group'])
    routing_policy = dict(kind='layerwise_balanced_group_limited_periodic',routing_seed=7,layer_stride=104729,
        communication_mode='token_rank',n_group=shape['n_group'],topk_group=shape['topk_group'],
        period_tokens=len(routing_pattern),experts_per_selected_group=2,
        scope='All four-of-eight group subsets and uniform expert marginals; no learned gate scores or real request routing')
    if max_context > shape["max_position_embeddings"]:
        raise ValueError("requested profile context exceeds the model context limit")
    if 256 % ep_size or 128 % tp_size or 2048 % tp_size or 7168 % tp_size:
        raise ValueError("DeepSeek dimensions/experts must divide TP/EP evenly")
    p,d = anchors["observations"]["prefill"],anchors["observations"]["decode"]
    for obs in (p,d):
        for key in ("tp_size","expert_timing_tp","ep_size","tokens","context_tokens","expert_assignments","active_experts"):
            number(obs[key],"anchor "+key,positive=True,integral=True)
        if set(obs["roles_seconds"]) != set(ROLES):
            raise ValueError("compute anchors must contain every required role")
        for value in obs["roles_seconds"].values():number(value,"anchor role time",positive=True)
        if obs['active_experts'] > 256//obs['ep_size'] or obs['active_experts'] > obs['expert_assignments']:
            raise ValueError('anchor active expert count exceeds its placement or assignment count')
    # Fit T = a * assignments + b * active_experts on the source TP-equivalent
    # work. Target TP divides the work, not the number of experts touched.
    np_,nd = p["expert_assignments"],d["expert_assignments"]
    xp,xd = p["active_experts"],d["active_experts"]
    yp=p["roles_seconds"]["expert"]*p["expert_timing_tp"]
    yd=d["roles_seconds"]["expert"]*d["expert_timing_tp"]
    determinant=np_*xd-nd*xp
    if determinant == 0:raise ValueError("expert anchors cannot identify assignment and active-expert costs")
    a=(yp*xd-yd*xp)/determinant
    b=(yd-a*nd)/xd
    if a <= 0 or b < 0 or not math.isfinite(a+b):
        raise ValueError("expert anchors do not support a nonnegative affine work model")
    floor=min(d["roles_seconds"]["norm"]/3, p["roles_seconds"]["norm"]/3)

    def linear(role,tokens):
        factor_p=p["tp_size"]/tp_size if role not in ("norm","router") else 1.
        factor_d=d["tp_size"]/tp_size if role not in ("norm","router") else 1.
        return max(floor,p["roles_seconds"][role]*tokens/p["tokens"]*factor_p,
                   d["roles_seconds"][role]*factor_d)

    def attention(query,cached,phase):
        if phase == "prefill":
            pairs=query*(cached+(query+1)/2)
            reference=p["tokens"]*(p["context_tokens"]+1)/2
            return max(floor,p["roles_seconds"]["attention_core"]*pairs/reference*p["tp_size"]/tp_size)
        return max(2*floor,d["roles_seconds"]["attention_core"]*query/d["tokens"]*
                   max(1,cached)/d["context_tokens"]*d["tp_size"]/tp_size)

    def dense_linear(query,is_dense):
        return linear("attention_projection",query)+linear("norm",query)+linear("dense_mlp" if is_dense else "shared_mlp",query)+(0. if is_dense else linear("router",query))

    def message(tokens):return tokens*shape["hidden_size"]*2
    contexts=_knots(max_context)
    query_knots=_knots(prefill_chunk_tokens)
    cached_knots=_knots(max_context,True)
    packed_knots=_knots(prefill_batch_token_budget)
    # Compressed MLA latent + rotary key are replicated on each TP GPU.
    # Existing executor divides this aggregate across TP ranks; include all TP
    # copies so physical memory and rank-to-rank KV sends are conserved.
    layer_kv=(shape["kv_lora_rank"]+shape["qk_rope_head_dim"])*2*tp_size

    def chunk_curve(is_dense):
        groups=[]
        for query in query_knots:
            points=[]
            for cached in cached_knots:
                attn=attention(query,cached,"prefill")
                points.append(dict(cached_tokens=cached,compute_seconds=dense_linear(query,is_dense)+attn,
                    embedding_seconds=0.,collective_bytes=message(query),attention_seconds=attn))
            groups.append(dict(chunk_tokens=query,points=points))
        return dict(collectives_per_layer=2,groups=groups,linear_points=[dict(tokens=t,
            compute_seconds=dense_linear(t,is_dense),embedding_seconds=0.,collective_bytes=message(t)) for t in packed_knots])

    layers=[]
    for index in range(61):
        layers.append(dict(kv_bytes_per_token=layer_kv,collectives_per_step=2,
            points=[dict(tokens=t,compute_seconds=dense_linear(t,index<3)+attention(t,0,"prefill"),
                         collective_bytes=message(t)) for t in contexts]))
    chunked=chunk_curve(False)
    chunked["layer_overrides"]=[dict(layer_ids=[0,1,2],profile=chunk_curve(True))]
    batches=[]
    for batch in _knots(decode_batch_size):
        cost=3*dense_linear(batch,True)+58*dense_linear(batch,False)
        batches.append(dict(batch_size=batch,points=[dict(tokens=c,
            compute_seconds=cost+61*attention(batch,c,"decode"),collective_bytes=message(batch)) for c in contexts]))
    decode_families={}
    for name,is_dense in (('dense',True),('moe',False)):
        family_batches=[dict(batch_size=batch,points=[dict(tokens=c,
            compute_seconds=dense_linear(batch,is_dense)+attention(batch,c,'decode'),
            collective_bytes=message(batch)) for c in contexts]) for batch in _knots(decode_batch_size)]
        decode_families[name]=dict(collectives_per_step=2,
            points=copy.deepcopy(family_batches[0]['points']),batches=family_batches)
    h=shape["hidden_size"];heads=shape["num_attention_heads"]
    attention_parameters=(h*1536+1536*heads*(128+64)+h*(512+64)+512*heads*(128+128)+heads*128*h)
    dense_parameters=3*3*h*18432
    shared_parameters=58*3*h*2048
    routed_parameters=58*256*3*h*2048
    embeddings=2*shape["vocab_size"]*h
    norms=61*(2*h+1536+512)+h
    local_fp8=(61*attention_parameters+dense_parameters+shared_parameters)/tp_size+routed_parameters/(tp_size*ep_size)
    # FP8 matrices and one FP32 scale per 128x128 block; BF16 embedding/norm.
    weights=math.ceil(local_fp8*(1+4/128**2)+embeddings*2/tp_size+norms*2)
    phase_weights = {phase: math.ceil(((61*attention_parameters+dense_parameters+shared_parameters)/tp_size+
        routed_parameters/(tp_size*size))*(1+4/128**2)+embeddings*2/tp_size+norms*2)
        for phase, size in phase_eps.items()}
    limitations=[
        "Simulation TP/EP, context and batch grids are analytically scaled from source anchors; no statistical error bound is inferred.",
        "CPU scheduling, sampling and embedding/final vocabulary projection costs are outside this simulation model.",
        "Excluded unmatched CUDA setup/packing/casting kernels are not silently counted as network-free compute; extraction lists the exclusions.",
        "Balanced periodic routing obeys the official 8-group/4-selected-group constraint; group scores, within-group scores and request-dependent expert hotspots remain synthetic.",
        "Layer-specific deterministic rotations route each token independently of chunk/batch scheduling; learned gate scores are not modeled.",
        "Activations are deduplicated per token/target GPU; local expert outputs combine before return. Local combine arithmetic has explicit zero uncalibrated cost; no inter-node aggregation/forwarding is modeled.",
        "MLA KV is BF16 and replicated per TP rank; this executor sends each copy rank-to-rank rather than deduplicating NIC payload.",
        "Decode executes each layer's non-expert compute/TP before its routed MoE; intra-layer kernels are aggregate costs and two-microbatch overlap is not reproduced.",
        "Affine expert costs omit cache residency, padding, launch saturation and interference; TP speedup is an estimate.",
        "FP8 allocation is a simulation estimate; workspace, activation buffers and MTP weights are outside the modeled capacity budget.",
    ]
    provenance=dict(synthetic=True,description="Estimated H800 DeepSeek-V3 TP/EP simulation cost profile derived from attributable offline compute anchors.",
        profile_kind="estimated_simulation_profile",target_model="deepseek-ai/DeepSeek-V3",device="NVIDIA H800",
        tensor_parallel_size=tp_size,expert_parallel_size=ep_size,model_config=file_identity(model_config_path),
        model_shape=expected,source_kind=anchors.get("source_kind"),sources=copy.deepcopy(anchors.get("sources",[])),
        source_observations=copy.deepcopy(anchors["observations"]),measurement_unit="seconds",exported_unit="seconds",
        timing_model=dict(linear="max(prefill per-token compute branch, decode weight/launch branch, measured norm floor); matrix work scales source_TP/target_TP; norm/router replicated",
            prefill_attention="Causal query/history pair count relative to source 4K prompts, plus a measured kernel floor",
            decode_attention="Batch*context relative to source, divided by target TP, plus a two-kernel floor",
            experts="Fit assignment work plus actual active-expert weight/launch work; divide work by target TP",
            expert_full_tp_seconds_per_assignment=a,expert_full_tp_seconds_per_active_expert=b),
        kv_layout="BF16 compressed MLA latent+rotary key replicated across TP ranks",routing="explicit balanced group-limited periodic top-k",
        routing_policy=routing_policy,
        coverage=dict(max_context=max_context,max_prefill_chunk_tokens=prefill_chunk_tokens,
            max_prefill_batch_tokens=prefill_batch_token_budget,max_decode_batch_size=decode_batch_size),
        limitations=limitations)
    profile=dict(schema_version=2,model="deepseek-ai/DeepSeek-V3",provenance=provenance,
        network_communication_excluded=True,weights_bytes_per_gpu=weights,kv_bytes_per_token=61*layer_kv,
        phases=dict(prefill=dict(tp_size=tp_size,layers=layers,chunked=chunked),
                    decode=dict(tp_size=tp_size,collectives_per_step=122,points=copy.deepcopy(batches[0]["points"]),batches=batches,
                        layers=['dense']*3+['moe']*58,layer_profiles=decode_families,step_overhead_seconds=0.)),
        expert_parallel=dict(activation_layout="tp_sharded",dense_compute_excludes_experts=True,
            num_experts=256,top_k=8,hidden_size=7168,dispatch_element_bytes=1,combine_element_bytes=2,
            n_group=shape['n_group'],topk_group=shape['topk_group'],
            routing_mode='layerwise_periodic',routing_seed=7,communication_mode='token_rank',
            local_combine_seconds_per_extra_assignment=0.,
            routing_pattern=routing_pattern,prefill_layer_ids=list(range(3,61)),decode_layers=58,
            expert_compute_seconds_per_assignment={phase:a/tp_size for phase in ("prefill","decode")},
            expert_compute_seconds_per_active_expert={phase:b/tp_size for phase in ("prefill","decode")},expert_tp_collectives_per_call=1))
    audit=dict(status="estimated_simulation_profile",modeling_scope="simulation_compute_costs",
        model="deepseek-ai/DeepSeek-V3",gpu="NVIDIA H800",tp_size=tp_size,ep_size=ep_size,
        layers=61,moe_layers=58,routed_experts=256,top_k=8,routing_policy=copy.deepcopy(routing_policy),
        parameter_count=61*attention_parameters+dense_parameters+shared_parameters+routed_parameters+embeddings+norms,
        weights_bytes_per_gpu=weights,weights_gib_per_gpu=weights/2**30,
        kv_bytes_per_token_per_gpu=61*layer_kv//tp_size,kv_bytes_per_token_per_tp_team=61*layer_kv,
        exported_coverage=provenance["coverage"],source_anchors=copy.deepcopy(anchors),limitations=limitations)
    if phase_layout:
        provenance.pop('expert_parallel_size')
        provenance.update(expert_parallel_sizes=phase_eps, expert_rank_unit='gpu' if tp_size == 1 else 'tp_team')
        profile.update(weights_bytes_per_gpu=max(phase_weights.values()), weights_bytes_per_gpu_by_phase=phase_weights)
        audit.update(ep_size=None, ep_sizes_by_phase=phase_eps, weights_bytes_per_gpu=max(phase_weights.values()),
            weights_gib_per_gpu=max(phase_weights.values())/2**30, weights_bytes_per_gpu_by_phase=phase_weights,
            weights_gib_per_gpu_by_phase={p: n/2**30 for p,n in phase_weights.items()})
    return profile,audit
