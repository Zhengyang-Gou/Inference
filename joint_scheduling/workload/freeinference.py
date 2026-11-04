"""FreeInference session JSONL -> observed, independent LLM requests.

Only compact request metadata survives parsing. Tool arguments, text and block
hashes never become execution inputs. This adapter does not simulate sessions.
"""
import hashlib
import json
from collections import Counter
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from .trace_values import nanoseconds, positive_integer


class TraceImportError(ValueError):
    """A parsed trace cannot be imported under the requested quality policy."""

    def __init__(self, message, audit):
        super().__init__(message)
        self.audit = audit


def _raw_chunks(path, identity):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131072), b""):
            digest.update(chunk)
            identity["bytes"] += len(chunk)
            yield chunk
    identity["sha256"] = digest.hexdigest()


def _decompressed_chunks(path, identity):
    with closing(_raw_chunks(path, identity)) as chunks:
        if path.suffix != ".zst":
            yield from chunks
            return
        try:
            import zstandard
        except ImportError as error:
            raise ValueError("FreeInference .zst requires zstandard; install the backend requirements or supply uncompressed JSONL") from error
        decoder, saw_data = None, False
        try:
            for chunk in chunks:
                saw_data = True
                while chunk:
                    if decoder is None:
                        decoder = zstandard.ZstdDecompressor().decompressobj()
                    decoded = decoder.decompress(chunk)
                    chunk = decoder.unused_data
                    if decoder.eof:
                        decoder = None
                    if decoded:
                        yield decoded
        except zstandard.ZstdError as error:
            raise ValueError("invalid zstd trace: {}".format(path.name)) from error
        if decoder is not None or not saw_data:
            raise ValueError("truncated or empty zstd trace: {}".format(path.name))


def _rows(path, identity):
    pending = b""
    line_number = 0
    for chunk in _decompressed_chunks(path, identity):
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        for line in lines:
            line_number += 1
            if not line.strip():
                raise ValueError("empty FreeInference JSONL record at {}:{}".format(path.name, line_number))
            yield line_number, json.loads(line, parse_float=Decimal)
    if pending:
        yield line_number + 1, json.loads(pending, parse_float=Decimal)


def _sources(path):
    if path.is_dir():
        paths = sorted(p for p in path.rglob("*") if p.is_file() and
                       (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.zst")))
    else:
        paths = [path]
    if not paths:
        raise ValueError("FreeInference source directory contains no .jsonl or .jsonl.zst files")
    identities = []
    for item in paths:
        identities.append(dict(path=item.relative_to(path).as_posix() if path.is_dir() else item.name,
                               sha256=None, bytes=0))
    return paths, identities


def read_freeinference(path, *, expected_sha256=None, invalid_requests="error"):
    """Read one file or a directory of raw session files, including subagents.

    Missing/nonpositive request lengths are rejected by default. Explicit skip
    mode records every exclusion. Duplicate sessions are errors, scoped by week.
    Arrival normalization includes observed timestamps of rejected requests.
    """
    if invalid_requests not in ("error", "skip"):
        raise ValueError("invalid_requests must be error or skip")
    path = Path(path)
    paths, identities = _sources(path)
    records, rejected, seen, models = [], [], set(), Counter()
    root_count = nested_count = request_count = 0
    first_time = last_time = None

    for source_path, identity in zip(paths, identities):
        for line, root in _rows(source_path, identity):
            root_count += 1
            stack = [(root, None, None, None, None)]
            while stack:
                session, parent_id, parent_step, parent_tool_index, root_id = stack.pop()
                if not isinstance(session, dict) or not isinstance(session.get("requests"), list):
                    raise ValueError("FreeInference records require a session object with requests")
                week, session_id = session.get("week", root.get("week")), session.get("session_id")
                if not isinstance(week, str) or not week.strip() or not isinstance(session_id, str) or not session_id.strip():
                    raise ValueError("FreeInference sessions require nonempty week and session_id")
                key = (week, session_id)
                if key in seen:
                    raise ValueError("duplicate FreeInference session in week {}: {}; supply non-overlapping source files".format(*key))
                seen.add(key)
                root_id = root_id or session_id
                nested_count += parent_id is not None
                children = []
                for request_index, request in enumerate(session["requests"]):
                    source_index = request_count
                    request_count += 1
                    metadata = dict(source_file=identity["path"], source_line=line,
                                    week=week, day=session.get("day", root.get("day")),
                                    session_id=session_id, root_session_id=root_id,
                                    parent_session_id=parent_id, parent_step=parent_step,
                                    parent_tool_call_index=parent_tool_index, request_index=request_index)
                    reasons = []
                    timestamp = None
                    inp = out = None
                    if not isinstance(request, dict):
                        reasons.append("invalid_request_record")
                    else:
                        for name in ("step", "turn_index", "step_trigger", "link_reason", "model"):
                            value = request.get(name)
                            if value is not None and type(value) not in (str, int):
                                raise ValueError("FreeInference {} must be scalar metadata".format(name))
                            metadata[name] = value
                        model = request.get("model")
                        models[str(model)] += 1
                        if request.get("start_ms") is None:
                            reasons.append("missing_start_ms")
                        else:
                            try:
                                if type(request["start_ms"]) is bool:
                                    raise ValueError("boolean timestamp")
                                timestamp = nanoseconds(request["start_ms"], 1000)
                            except ValueError:
                                reasons.append("invalid_start_ms")
                        tokens = request.get("provider_tokens")
                        if not isinstance(tokens, dict):
                            reasons.append("invalid_provider_tokens")
                        else:
                            for field in ("prompt", "completion"):
                                if tokens.get(field) is None:
                                    reasons.append("missing_{}_tokens".format(field))
                                    continue
                                try:
                                    value = positive_integer(tokens[field], field)
                                    if field == "prompt":
                                        inp = value
                                    else:
                                        out = value
                                except ValueError:
                                    reasons.append("invalid_{}_tokens".format(field))
                            cached = tokens.get("cached")
                            if type(cached) is int and cached >= 0:
                                metadata["provider_cached_tokens"] = cached
                        tools = request.get("tool_calls", [])
                        if not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools):
                            raise ValueError("FreeInference tool_calls must be a list of objects")
                        for index, tool in enumerate(tools):
                            child = tool.get("subagent")
                            if child is not None:
                                if not isinstance(child, dict):
                                    raise ValueError("FreeInference subagent must be a session object or null")
                                children.append((child, session_id, request.get("step"), index, root_id))
                    if timestamp is not None:
                        metadata["source_start_ns"] = timestamp
                        first_time = timestamp if first_time is None else min(first_time, timestamp)
                        last_time = timestamp if last_time is None else max(last_time, timestamp)
                    if reasons:
                        rejected.append(dict(source_index=source_index, reasons=reasons, **metadata))
                    else:
                        records.append(dict(source_index=source_index, source_time_ns=timestamp,
                                            input_tokens=inp, output_tokens=out, metadata=metadata))
                stack.extend(reversed(children))

    if path.is_dir():
        encoded = json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
    else:
        digest = identities[0]["sha256"]
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("trace SHA-256 does not match the frozen source")
    reason_counts = Counter(reason for row in rejected for reason in row["reasons"])
    audit = dict(schema_version=1, source_format="freeinference", source_sha256=digest,
                 source_files=identities, invalid_request_policy=invalid_requests,
                 source_request_count=request_count, imported_request_count=len(records),
                 rejected_request_count=len(rejected), rejection_reason_counts=dict(reason_counts),
                 root_session_count=root_count, nested_session_count=nested_count,
                 models=dict(models), rejected_requests=rejected,
                 session_dependencies_modeled=False, prefix_reuse_enabled_by_import=False)
    if rejected and invalid_requests == "error":
        raise TraceImportError("{} FreeInference requests have invalid timing/lengths; use --invalid-requests skip for an explicitly audited subset".format(len(rejected)), audit)
    if not records:
        raise TraceImportError("FreeInference trace contains no importable requests", audit)
    provenance = dict(source_files=identities,
                      source_identity_kind="sorted relative-path/size/SHA-256 manifest" if path.is_dir() else "file SHA-256",
                      source_request_count=request_count, imported_request_count=len(records),
                      dropped_requests=len(rejected), rejection_reason_counts=dict(reason_counts),
                      invalid_request_policy=invalid_requests, root_session_count=root_count,
                      nested_session_count=nested_count,
                      source_origin_ns=first_time, source_duration_ns=last_time - first_time + 1,
                      token_definition="provider prompt/completion; cached prompt tokens are not subtracted",
                      prefix_hashes_imported=False, replay_semantics="open-loop observed LLM request arrivals")
    return dict(records=records, provenance=provenance, audit=audit, source_sha256=digest)
