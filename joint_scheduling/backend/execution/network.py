"""NetworkExecutor: explicit runtime component; scheduling order preserved."""
import math
from ...feedback import NetworkTransfer
from .component import RuntimeComponent


class NetworkExecutor(RuntimeComponent):
    owned_fields = frozenset(['collective_counter', 'collective_demands', 'transfer_counter'])

    def init_network_state(self):
        self.collective_demands = {}
        self.collective_counter = 0
        self.transfer_counter = 0

    def transfer(self, rid, kind, src, dst, size, complete, priority=None, **details):
        self.transfer_counter += 1
        priority = (1 if kind=='collective' else 2) if priority is None else priority
        transfer = NetworkTransfer("transfer-{}".format(self.transfer_counter), kind, src, dst, size, priority)
        local = src // self.config.topology.gpus_per_node == dst // self.config.topology.gpus_per_node
        self.observer.transfer_started(transfer, "local" if local else "inter_node")
        self.emit("transfer_started", request_id=rid, transfer_id=transfer.transfer_id, transfer_kind=kind,
                  source_gpu_id=src, destination_gpu_id=dst, size_bytes=size, priority=priority, **details)
        def finished(progress):
            self.observer.transfer_completed(transfer.transfer_id)
            self.emit("transfer_completed", request_id=rid, transfer_id=transfer.transfer_id, transfer_kind=kind,
                      source_reads_complete=progress.source_reads_complete, destination_ready=progress.destination_ready,
                      **details)
            complete()
        self.backend.submit(transfer, finished)
        return transfer.transfer_id

    def allreduce(self, rid, ranks, size, complete):
        if len(ranks) == 1:
            complete()
            return
        rounds = 2 * (len(ranks) - 1)
        self.collective_counter += 1
        collective_id = "collective-{}".format(self.collective_counter)
        started = self.backend.now_seconds()
        self.emit("collective_started", request_id=rid, collective_id=collective_id,
                  collective_type="tp_allreduce", gpu_ids=list(ranks))
        chunk = math.ceil(size / len(ranks))
        def step(index):
            if index == rounds:
                self.emit("collective_completed", request_id=rid, collective_id=collective_id,
                          collective_type="tp_allreduce", duration_seconds=self.backend.now_seconds()-started)
                complete()
                return
            pending = [len(ranks)]
            def done():
                pending[0] -= 1
                if pending[0] == 0:
                    step(index + 1)
            for rank, src in enumerate(ranks):
                self.transfer(rid, "collective", src, ranks[(rank + 1) % len(ranks)], chunk, done,
                              collective_type="tp_allreduce")
        step(0)
