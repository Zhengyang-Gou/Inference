"""Pure local scheduling rules shared by execution and binding forecasts."""


def prefill_members(queued, runtime, admit):
    """Running first, then waiting FCFS; stop at the first blocked admission.

    ``admit`` may initialize a cache-aware cursor before query allocation. In
    prediction it touches detached state only; in execution it reserves memory.
    """
    running = sorted((row for row in queued if row.get("prefill_started")),
                     key=lambda row: row.get("prefill_admission_sequence", 0))
    waiting = [row for row in queued if not row.get("prefill_started")]
    count, left, members = len(running), runtime.prefill_batch_token_budget, []
    for row in running+waiting:
        if left == 0:
            break
        new = not row.get("prefill_started")
        if new and count >= runtime.prefill_batch_size:
            break
        if new:
            if not admit(row):
                return members, True
            count += 1
        query = min(left, runtime.prefill_chunk_tokens,
                    row["input_tokens"]-row["prefill_computed_tokens"])
        if query <= 0:
            raise RuntimeError("completed prefill remained in scheduling queue")
        members.append(dict(request_id=row["request_id"], chunk_tokens=query,
                            cached_tokens=row["prefill_computed_tokens"], new_admission=new))
        left -= query
    return members, False
