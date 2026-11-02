"""Compute-only profile lookup; no clock, network or request mutation."""
import math
from pathlib import Path
from ..validation import require_mapping
from .inputs import read_json, number


def profile_kind(provenance):
    """Classify an offline simulation input without deployment prerequisites."""
    return provenance.get('profile_kind',
        'synthetic_profile' if provenance['synthetic'] else 'source_profile')


class ComputeProfile:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.raw = require_mapping(read_json(path), "profile")
        if type(self.raw.get("schema_version")) is not int or self.raw["schema_version"] not in (1, 2):
            raise ValueError("unsupported compute profile schema")
        self.layered = self.raw["schema_version"] == 2
        self.provenance = require_mapping(self.raw.get("provenance"), "profile.provenance")
        if type(self.provenance.get("synthetic")) is not bool or not isinstance(self.provenance.get("description"), str):
            raise ValueError("profile provenance must explicitly declare synthetic and description")
        if self.raw.get("network_communication_excluded") is not True:
            raise ValueError("compute profile must exclude explicitly simulated communication")
        self.kv_bytes_per_token = number(self.raw.get("kv_bytes_per_token"), "kv_bytes_per_token", True, True)
        self.weights_bytes_per_gpu = number(self.raw.get("weights_bytes_per_gpu"), "weights_bytes_per_gpu", integral=True)
        weights = self.raw.get('weights_bytes_per_gpu_by_phase')
        if weights is not None and (not isinstance(weights, dict) or set(weights) != {'prefill', 'decode'}):
            raise ValueError('phase weights require prefill and decode')
        self.weights_bytes_per_gpu_by_phase = {phase: number(
            weights[phase] if weights is not None else self.weights_bytes_per_gpu,
            'weights_bytes_per_gpu_by_phase.'+phase, integral=True) for phase in ('prefill', 'decode')}
        if weights is not None and self.weights_bytes_per_gpu != max(self.weights_bytes_per_gpu_by_phase.values()):
            raise ValueError('legacy weight scalar must equal the maximum phase weight')
        self.phases = require_mapping(self.raw.get("phases"), "profile.phases")
        for phase in ("prefill", "decode"):
            values = require_mapping(self.phases.get(phase), "profile.phases." + phase)
            number(values.get("tp_size"), "tp_size", True, True)
            if phase == "prefill" and self.layered:
                if not {"tp_size", "layers"} <= set(values) or set(values)-{"tp_size", "layers", "chunked"} or not isinstance(values["layers"], list) or not values["layers"]:
                    raise ValueError("schema 2 prefill requires tp_size and nonempty layers")
                total_kv = 0
                for layer in values["layers"]:
                    layer = require_mapping(layer, "prefill layer")
                    if not {"kv_bytes_per_token", "collectives_per_step", "points"} <= set(layer) or set(layer) - {"kv_bytes_per_token", "collectives_per_step", "points", "cached_points"}:
                        raise ValueError("prefill layer requires kv_bytes_per_token, collectives_per_step and points")
                    total_kv += number(layer["kv_bytes_per_token"], "layer kv_bytes_per_token", True, True)
                    self._validate_samples(layer)
                    previous_cached = 0
                    if not isinstance(layer.get("cached_points", []), list):
                        raise ValueError("cached_points must be a list")
                    for group in layer.get("cached_points", []):
                        if not isinstance(group, dict) or set(group) != {"cached_tokens", "points"}:
                            raise ValueError("cached_points require cached_tokens and points")
                        cached = number(group["cached_tokens"], "cached_tokens", True, True)
                        if cached <= previous_cached:
                            raise ValueError("cached token groups must be unique and increasing")
                        self._validate_samples(dict(group, collectives_per_step=layer["collectives_per_step"]))
                        if any(p["tokens"] <= cached for p in group["points"]):
                            raise ValueError("cached profile total tokens must exceed cached_tokens")
                        previous_cached = cached
                if total_kv != self.kv_bytes_per_token:
                    raise ValueError("sum of layer KV bytes must equal kv_bytes_per_token")
            else:
                if "layers" in values:
                    raise ValueError("layer profiles require schema_version 2 and the prefill phase")
                self._validate_samples(values)
        chunked = self.phases["prefill"].get("chunked")
        self.chunked_by_layer = {}
        if chunked is not None:
            if not self.layered:
                raise ValueError("chunked prefill requires schema 2")
            from .chunked_prefill import validate_chunked
            validate_chunked(chunked, number)
            for override in chunked.get("layer_overrides", []):
                for index in override["layer_ids"]:
                    if index >= len(self.phases["prefill"]["layers"]):
                        raise ValueError("chunk override layer is outside the compute profile")
                    self.chunked_by_layer[index] = override["profile"]
        batches = self.phases["decode"].get("batches", [])
        if not isinstance(batches, list):
            raise ValueError("decode batches must be a list")
        previous = 0
        for batch in batches:
            if not isinstance(batch, dict) or set(batch) != {"batch_size", "points"}:
                raise ValueError("decode batches require batch_size and points")
            size = number(batch["batch_size"], "batch_size", True, True)
            if size <= previous:
                raise ValueError("decode batch sizes must be unique and increasing")
            self._validate_samples(dict(batch, collectives_per_step=self.phases["decode"]["collectives_per_step"]))
            previous = size
        if batches and (batches[0]["batch_size"] != 1 or batches[0]["points"] != self.phases["decode"]["points"]):
            raise ValueError("decode batch size 1 must match legacy decode points")
        self.expert = None
        if "expert_parallel" in self.raw:
            if not self.layered:
                raise ValueError("EP requires a layered schema 2 compute profile")
            from .expert_parallel import ExpertProfile
            self.expert = ExpertProfile(self.raw["expert_parallel"], len(self.phases["prefill"]["layers"]))

    def weights_for_phase(self, phase):
        return self.weights_bytes_per_gpu_by_phase[phase]

    def prefill_chunks(self, tokens, tp_size, chunk_tokens, cached_tokens=0):
        number(tokens, "prefill tokens", True, True)
        number(chunk_tokens, "prefill chunk size", True, True)
        number(cached_tokens, "prefill cached tokens", integral=True)
        if cached_tokens >= tokens:
            raise ValueError("cached prefill must leave a query to produce logits")
        if tp_size != self.phases["prefill"]["tp_size"]:
            raise ValueError("prefill TP size does not match profile")
        raw = self.phases["prefill"].get("chunked")
        if not self.layered or raw is None:
            raise ValueError("chunked prefill requires explicit cached-context chunk timing")
        chunks = []
        for cached in range(cached_tokens, tokens, chunk_tokens):
            size = min(chunk_tokens, tokens-cached)
            steps = self.prefill_chunk_steps(size, cached, tp_size)
            chunks.append(dict(cached_tokens=cached, chunk_tokens=size, steps=steps))
        return tuple(chunks)

    def prefill_chunk_steps(self, query_tokens, cached_tokens, tp_size):
        number(query_tokens, "chunk tokens", True, True)
        number(cached_tokens, "cached tokens", integral=True)
        if tp_size != self.phases["prefill"]["tp_size"]:
            raise ValueError("prefill TP size does not match profile")
        raw = self.phases["prefill"].get("chunked")
        if not self.layered or raw is None:
            raise ValueError("chunked prefill requires explicit cached-context chunk timing")
        from .chunked_prefill import interpolate_chunk
        steps=[]
        for index,layer in enumerate(self.phases["prefill"]["layers"]):
            curve=self.chunked_by_layer.get(index,raw)
            sample=interpolate_chunk(curve,query_tokens,cached_tokens)
            steps.append(dict(layer_id=index,kv_bytes_per_token=layer["kv_bytes_per_token"],
                compute_seconds=sample["compute_seconds"]+(sample["embedding_seconds"] if index==0 else 0),
                collective_bytes=sample["collective_bytes"],collectives_per_step=curve["collectives_per_layer"]))
        return tuple(steps)

    def prefill_batch_steps(self, chunks, tp_size):
        """Packed linear rows, isolated per-sequence attention, one collective per layer."""
        if not chunks:
            raise ValueError("prefill batch must be nonempty")
        if tp_size != self.phases["prefill"]["tp_size"]:
            raise ValueError("prefill TP size does not match profile")
        raw = self.phases["prefill"].get("chunked")
        if not self.layered or raw is None or "linear_points" not in raw:
            raise ValueError("prefill batching requires decomposed linear/attention timing")
        from .chunked_prefill import interpolate_chunk,interpolate_linear
        total=0
        for chunk in chunks:
            query=number(chunk["chunk_tokens"],"chunk tokens",True,True)
            cached=number(chunk["cached_tokens"],"cached tokens",integral=True)
            total+=query
        steps=[];samples={}
        for index,layer in enumerate(self.phases["prefill"]["layers"]):
            curve=self.chunked_by_layer.get(index,raw)
            if id(curve) not in samples:
                attention=sum(interpolate_chunk(curve,c['chunk_tokens'],c['cached_tokens'])["attention_seconds"] for c in chunks)
                samples[id(curve)]=(interpolate_linear(curve,total),attention)
            linear,attention=samples[id(curve)]
            steps.append(dict(layer_id=index,kv_bytes_per_token=layer["kv_bytes_per_token"],
                compute_seconds=linear["compute_seconds"]+attention+(linear["embedding_seconds"] if index==0 else 0),
                collective_bytes=linear["collective_bytes"],collectives_per_step=curve["collectives_per_layer"]))
        return tuple(steps)

    def validate_prefill_batch_coverage(self, max_input_tokens, query_cap, token_budget, tp_size):
        self.prefill_batch_steps([dict(chunk_tokens=1,cached_tokens=0)],tp_size)
        from .chunked_prefill import interpolate_linear,interpolate_chunk
        raw=self.phases["prefill"]["chunked"]
        largest=min(max_input_tokens,query_cap,token_budget)
        # Validate the bounding rectangle. It is deliberately stricter than the
        # request's triangular query/history domain; no mid-run extrapolation.
        for curve in [raw]+[o['profile'] for o in raw.get('layer_overrides',[])]:
            interpolate_linear(curve,token_budget)
            interpolate_chunk(curve,largest,max_input_tokens-1)
            for group in curve["groups"]:
                if group["chunk_tokens"]<=largest:
                    interpolate_chunk(curve,group["chunk_tokens"],max_input_tokens-1)

    def lookup_decode_batch(self, contexts, tp_size):
        """Padded maximum context, interpolation in context then batch size; no extrapolation."""
        if not contexts:
            raise ValueError("decode batch must be nonempty")
        for context in contexts:
            number(context, "decode context", True, True)
        size, tokens = len(contexts), max(contexts)
        if size == 1:
            return self.lookup("decode", tokens, tp_size)
        config = self.phases["decode"]
        if tp_size != config["tp_size"]:
            raise ValueError("decode TP size does not match profile")
        batches = config.get("batches", [])
        if not batches or size > batches[-1]["batch_size"]:
            raise ValueError("decode batch size {} is outside declared batch coverage".format(size))
        lo = max((b for b in batches if b["batch_size"] <= size), key=lambda b: b["batch_size"])
        hi = min((b for b in batches if b["batch_size"] >= size), key=lambda b: b["batch_size"])
        samples = [self._interpolate(dict(b, collectives_per_step=config["collectives_per_step"]), tokens, "decode batch") for b in (lo, hi)]
        fraction = 0 if lo is hi else (size-lo["batch_size"])/(hi["batch_size"]-lo["batch_size"])
        return dict(compute_seconds=samples[0]["compute_seconds"]+fraction*(samples[1]["compute_seconds"]-samples[0]["compute_seconds"]),
                    collective_bytes=math.ceil(samples[0]["collective_bytes"]+fraction*(samples[1]["collective_bytes"]-samples[0]["collective_bytes"])),
                    collectives_per_step=config["collectives_per_step"])

    @staticmethod
    def _validate_samples(values):
        number(values.get("collectives_per_step"), "collectives_per_step", integral=True)
        points = values.get("points")
        if not isinstance(points, list) or not points:
            raise ValueError("profile phases need at least one point")
        previous = 0
        for point in points:
            point = require_mapping(point, "profile point")
            if set(point) != {"tokens", "compute_seconds", "collective_bytes"}:
                raise ValueError("profile points require tokens, compute_seconds and collective_bytes")
            number(point["tokens"], "tokens", True, True)
            number(point["compute_seconds"], "compute_seconds", True)
            number(point["collective_bytes"], "collective_bytes", integral=True)
            if point["tokens"] <= previous:
                raise ValueError("profile points must have unique increasing token counts")
            if values["collectives_per_step"] and point["collective_bytes"] == 0:
                raise ValueError("enabled collectives must have a positive message size")
            previous = point["tokens"]

    def lookup(self, phase, tokens, tp_size):
        config = self.phases[phase]
        if tp_size != config["tp_size"]:
            raise ValueError("{} TP size does not match profile".format(phase))
        if phase == "prefill" and self.layered:
            raise ValueError("layered prefill must use prefill_steps, not an aggregate communication sample")
        return self._interpolate(config, tokens, phase)

    def prefill_steps(self, tokens, tp_size, cached_tokens=0):
        if type(cached_tokens) is not int or not 0 <= cached_tokens < tokens:
            raise ValueError("cached compute context must be in [0, input_tokens)")
        if not self.layered:
            if cached_tokens:
                raise ValueError("prefix compute reuse requires explicit schema 2 cached_points")
            return (dict(self.lookup("prefill", tokens, tp_size), layer_id=0,
                         kv_bytes_per_token=self.kv_bytes_per_token),)
        if tp_size != self.phases["prefill"]["tp_size"]:
            raise ValueError("prefill TP size does not match profile")
        return tuple(dict(self._cached_sample(layer, tokens, cached_tokens, index),
                          layer_id=index, kv_bytes_per_token=layer["kv_bytes_per_token"])
                     for index, layer in enumerate(self.phases["prefill"]["layers"]))

    def _cached_sample(self, layer, tokens, cached_tokens, index):
        if not cached_tokens:
            return self._interpolate(layer, tokens, "prefill layer {}".format(index))
        groups = layer.get("cached_points", [])
        if not groups or cached_tokens > groups[-1]["cached_tokens"]:
            raise ValueError("prefix cache hit is outside explicit cached_points coverage")
        left = dict(cached_tokens=0, points=layer["points"])
        right = None
        for group in groups:
            if group["cached_tokens"] <= cached_tokens:
                left = group
            if group["cached_tokens"] >= cached_tokens:
                right = group
                break
        if left["cached_tokens"] == cached_tokens:
            right = left
        values = [self._interpolate(dict(g, collectives_per_step=layer["collectives_per_step"]), tokens,
                                   "cached prefill layer {}".format(index)) for g in (left, right)]
        fraction = 0 if left is right else (cached_tokens-left["cached_tokens"])/(right["cached_tokens"]-left["cached_tokens"])
        return dict(compute_seconds=values[0]["compute_seconds"] + fraction*(values[1]["compute_seconds"]-values[0]["compute_seconds"]),
                    collective_bytes=math.ceil(values[0]["collective_bytes"] + fraction*(values[1]["collective_bytes"]-values[0]["collective_bytes"])),
                    collectives_per_step=layer["collectives_per_step"])

    @staticmethod
    def _interpolate(config, tokens, phase):
        points = config["points"]
        if tokens < points[0]["tokens"] or tokens > points[-1]["tokens"]:
            raise ValueError("{} token count {} is outside declared profile coverage [{}, {}]".format(
                phase, tokens, points[0]["tokens"], points[-1]["tokens"]))
        left = right = points[0]
        for point in points:
            if point["tokens"] <= tokens:
                left = point
            if point["tokens"] >= tokens:
                right = point
                break
        fraction = 0 if left is right else (tokens - left["tokens"]) / (right["tokens"] - left["tokens"])
        return {"compute_seconds": left["compute_seconds"] + fraction * (right["compute_seconds"] - left["compute_seconds"]),
                "collective_bytes": math.ceil(left["collective_bytes"] + fraction * (right["collective_bytes"] - left["collective_bytes"])),
                "collectives_per_step": config["collectives_per_step"]}
