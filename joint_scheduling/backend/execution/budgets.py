"""BudgetEstimator: explicit runtime component; scheduling order preserved."""
from ..slo import MLUBudgetMixin
from ...kv_scheduler.adaptive import RequestBudget
from .component import RuntimeComponent


class BudgetEstimator(MLUBudgetMixin, RuntimeComponent):
    owned_fields = frozenset(['slo_budget_cache'])

    def collective_seconds(self, ranks, sample):
        work = self.scheduler.resources.ring_work(ranks, sample["collective_bytes"])
        if not work or not sample["collectives_per_step"]:
            return 0.0
        seconds = max(size / (self.scheduler.resources.capacities[key] *
                      (1-self.config.kv_scheduler.safety_margin_fraction)) for key, size in work.items())
        latency = 2 * (len(ranks)-1) * self.config.topology.link_latency_us * 4 / 1e6
        if self.config.topology.intra_node_model == 'gpu_ports' and len({g // self.config.topology.gpus_per_node for g in ranks}) == 1:
            latency = 2 * (len(ranks)-1) * self.config.topology.intra_node_latency_us / 1e6
        return sample["collectives_per_step"] * (seconds+latency)

    def decode_step_seconds(self, row):
        ranks = self.instances[row["d"]].gpu_ids
        sample = self.profile.lookup("decode", row["input_tokens"] + max(0, row["generated"]-1), len(ranks))
        seconds = sample["compute_seconds"] + self.collective_seconds(ranks, sample)
        if self.ep_enabled:
            seconds += self.ep_estimate_seconds(row['d'], 1, self.profile.expert.raw['decode_layers'])
        return seconds

    def request_budget(self, row, network):
        now = self.backend.now_seconds()
        ttft_slo = row.get("ttft_slo_seconds", self.config.evaluation.default_ttft_slo_seconds)
        itl_slo = row.get("itl_slo_seconds", self.config.evaluation.default_itl_slo_seconds)
        ttft_deadline = row["arrived_at_seconds"] + ttft_slo
        if row["prefill_finished"]:
            predicted_first = row["first_token_at_seconds"]
        elif self.prefill_batching or row["prefill_chunks"]:
            # Remaining work includes every future chunk and all currently visible
            # peers on this P. This deliberately bounds RR waiting conservatively.
            predicted_first = now + sum(self.prefill_remaining_seconds(peer)
                for peer in self.requests.values() if peer["p"] == row["p"]
                and peer["state"] in ("prefill", "queued_prefill"))
        else:
            index = row["prefill_layer_index"]
            sample = row["prefill_steps"][index]
            count = sample["collectives_per_step"]
            segment = row.get("segment_index", 0)
            part = sample["compute_seconds"] / max(1, count)
            left_compute = max(0, row.get("compute_segment_end", now)-now) + max(0, count-segment-1) * part
            current = left_compute + self.collective_seconds(self.instances[row["p"]].gpu_ids,
                dict(sample, collectives_per_step=max(0, count-segment)))
            future = sum(step["compute_seconds"] + self.collective_seconds(self.instances[row["p"]].gpu_ids, step)
                         for step in row["prefill_steps"][index+1:])
            predicted_first = now+current+future
        if self.ep_enabled and not row['prefill_finished']:
            query = max(1, row['input_tokens']-row.get('prefill_computed_tokens', 0))
            predicted_first += self.ep_estimate_seconds(row['p'], query, len(self.profile.expert.raw['prefill_layer_ids']))
        queued = list(self.queues[row["d"]])
        size = self.config.runtime.decode_batch_size
        queue_seconds = 0
        for offset in range(0, len(queued), size):
            members = [self.requests[rid] for rid in queued[offset:offset+size]]
            sample = self.profile.lookup_decode_batch(
                [r["input_tokens"]+max(0,r["generated"]-1) for r in members], len(self.instances[row["d"]].gpu_ids))
            queue_seconds += sample["compute_seconds"] + self.collective_seconds(self.instances[row["d"]].gpu_ids, sample)
            if self.ep_enabled:
                queue_seconds += self.ep_estimate_seconds(row['d'], len(members), self.profile.expert.raw['decode_layers'])
        active = self.active_by_instance.get(row["d"])
        if active is not None:
            members = self.active_decode_batches.get(row["d"], (active,))
            sample = self.profile.lookup_decode_batch([self.requests[rid]["input_tokens"]+max(0,self.requests[rid]["generated"]-1) for rid in members], len(self.instances[row["d"]].gpu_ids))
            queue_seconds += sample["compute_seconds"] + self.collective_seconds(self.instances[row["d"]].gpu_ids, sample)
            if self.ep_enabled:
                queue_seconds += self.ep_estimate_seconds(row['d'], len(members), self.profile.expert.raw['decode_layers'])
        latest = min(predicted_first, ttft_deadline) + itl_slo - queue_seconds - self.decode_step_seconds(row)
        # After the first token the handoff deadline is anchored to its actual
        # return time, including a missed TTFT rather than moving it forward.
        if row["prefill_finished"]:
            latest = predicted_first + itl_slo - queue_seconds - self.decode_step_seconds(row)
        partial = {gpu: 0 for gpu in row["kv_received_by_gpu"]}
        if network is not None:
            for flow in network["flows"]:
                active_transfer = row["kv_active_transfers"].get(flow["logical_id"])
                if active_transfer is not None:
                    partial[active_transfer[0]] += min(flow["acked_bytes"], active_transfer[1])
        work, remaining = {}, 0
        p, d = self.instances[row["p"]], self.instances[row["d"]]
        for rank, (src, dst) in enumerate(zip(p.gpu_ids, d.gpu_ids)):
            size = max(0, row["kv_rank_bytes"][rank] - row["kv_received_by_gpu"][dst] - partial[dst])
            remaining += size
            for key in self.scheduler.resources.for_gpus(src, dst):
                work[key] = work.get(key, 0) + size
        transfer_seconds = max((size/self.scheduler.estimate_rate(key) for key, size in work.items()), default=0)
        slack = latest-max(predicted_first, now+transfer_seconds)
        budget = RequestBudget(row["arrived_at_seconds"], ttft_deadline, predicted_first, latest, remaining,
                             transfer_seconds, slack, slack/itl_slo, predicted_first <= ttft_deadline+1e-12,
                             decode_instance_id=row["d"], final_layer_id=len(row["prefill_steps"])-1,
                             remaining_resource_bytes=work)
        return self.mlu_budget(row,budget) if self.mlu_enabled else budget

    def prefill_remaining_seconds(self, row):
        if self.prefill_batching:
            return self.batched_prefill_remaining_seconds(row)
        ranks = self.instances[row["p"]].gpu_ids
        total = 0
        for index, chunk in enumerate(row["prefill_chunks"][row["prefill_chunk_index"]:]):
            steps = chunk["steps"]
            if index == 0 and row["state"] == "prefill":
                layer = row.get("prefill_layer_index", 0)
                sample = steps[layer]
                count = sample["collectives_per_step"]
                segment = row.get("segment_index", 0)
                part = sample["compute_seconds"]/max(1,count)
                total += max(0,row.get("compute_segment_end",0)-self.backend.now_seconds())
                total += max(0,count-segment-1)*part
                total += self.collective_seconds(ranks,dict(sample,collectives_per_step=max(0,count-segment)))
                steps = steps[layer+1:]
            total += sum(step["compute_seconds"]+self.collective_seconds(ranks,step) for step in steps)
        return total
