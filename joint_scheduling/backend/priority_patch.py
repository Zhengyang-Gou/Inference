"""Reviewed priority overlay applied only to the isolated, pinned ns-3 build."""
from pathlib import Path


def apply_priority_overlay(source, upstream):
    def edit(relative, replacements):
        original = (Path(upstream)/'simulation'/relative).read_text()
        # qbb-net-device.cc also receives the independent PFC Resume correction.
        path = Path(source)/relative
        text = path.read_text() if relative.endswith('qbb-net-device.cc') else original
        for old, new in replacements:
            if text.count(old) != 1:
                raise ValueError('Pinned priority overlay no longer matches: '+relative)
            text = text.replace(old,new)
        path.write_text(text)

    edit('src/network/utils/broadcom-egress-queue.h', [
        ('static const unsigned qCnt = 8;', 'static bool strictPriority;\n\t\tstatic const unsigned qCnt = 8;')])
    edit('src/network/utils/broadcom-egress-queue.cc', [
        ('\tBEgressQueue::BEgressQueue()', '\tbool BEgressQueue::strictPriority = false;\n\tBEgressQueue::BEgressQueue()'),
        ('\t\t\tif (!found)\n', '''\t\t\tif (strictPriority)
            {
                for (qIndex = 1; qIndex < qCnt; ++qIndex) {
                    if (!paused[qIndex] && m_queues[qIndex]->GetNPackets() > 0) {
                        found = true;
                        break;
                    }
                }
            }
            else if (!found)
''')])
    edit('src/point-to-point/model/qbb-net-device.h', [
        ('static uint32_t ack_q_idx;', 'static bool strictPriority;\n\tstatic uint32_t ack_q_idx;')])
    edit('src/point-to-point/model/qbb-net-device.cc', [
        ('uint32_t RdmaEgressQueue::ack_q_idx = 3;', 'bool RdmaEgressQueue::strictPriority = false;\n\tuint32_t RdmaEgressQueue::ack_q_idx = 3;'),
        ('\t\t\t\tres = idx;\n\t\t\t\tbreak;', '''                // Round robin within the best eligible priority, respecting PFC,
                // congestion-control pacing and the sender window.
                if (res == -1024 || !strictPriority || qp->m_pg < m_qpGrp->Get(res)->m_pg)
                    res = idx;
                if (!strictPriority) break;''')])
