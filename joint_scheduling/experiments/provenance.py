"""Freeze executable source and identify each run independently of Git dirtiness."""
import hashlib
import fnmatch
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

from ..backend.build import REPO
from ..backend.compute_profile import profile_kind


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')


def _export_paths(excluded):
    """Source tar exports have no .git; honor this repository's ignore rules."""
    ignore=REPO/'.gitignore'
    patterns=ignore.read_text().splitlines() if ignore.exists() else []
    def ignored(name):
        result=False
        for pattern in patterns:
            if not pattern or pattern.startswith('#'):
                continue
            negative=pattern.startswith('!');pattern=pattern.lstrip('!').strip('/')
            parts=name.split('/')
            candidates=['/'.join(parts[:n]) for n in range(1,len(parts)+1)] if '/' in pattern else parts
            if any(fnmatch.fnmatchcase(p,pattern) for p in candidates):result=not negative
        return result
    paths=[]
    for parent,dirs,files in os.walk(REPO):
        for name in list(dirs)+files:
            path=Path(parent)/name;rel=path.relative_to(REPO).as_posix()
            skip=name=='.git' or ignored(rel) or any(path.resolve().is_relative_to(p) for p in excluded)
            if name in dirs and (skip or path.is_symlink()):dirs.remove(name)
            if not skip and (path.is_file() or path.is_symlink()):paths.append(rel)
    return sorted(paths)


def freeze_source(output, excluded_roots=()):
    """Archive tracked + nonignored untracked source; generated outputs excluded."""
    output = Path(output)
    excluded = [output.resolve(), *(Path(p).resolve() for p in excluded_roots)]
    output.mkdir(parents=True, exist_ok=False)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=REPO,stderr=subprocess.PIPE)
    try:
        if not (REPO/'.git').exists():
            raise FileNotFoundError('source export has no repository metadata')
        paths = sorted(set(p for p in git('ls-files', '-c', '-o', '--exclude-standard', '-z').decode().split('\0') if p))
        head=git('rev-parse','HEAD').decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths=_export_paths(excluded);head=None
    files = []
    with tarfile.open(output/'source.tar.gz', 'w:gz') as archive:
        for name in paths:
            path = REPO/name
            if any(path.resolve().is_relative_to(p) for p in excluded):
                continue
            if not path.exists() and not path.is_symlink():
                files.append(dict(path=name, missing=True))
                continue
            data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            files.append(dict(path=name, sha256=hashlib.sha256(data).hexdigest(),
                mode=path.lstat().st_mode & 0o777, symlink=path.is_symlink()))
            info = archive.gettarinfo(str(path), arcname=name)
            if path.is_symlink():
                info.linkname = data.decode()
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    identity = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    report = dict(schema_version=1, git_head=head,
        source_tree_sha256=identity, archive_sha256=sha256(output/'source.tar.gz'), files=files,
        scope='Tracked and nonignored untracked source, including dirty files; ignored build/results excluded.' if head
            else 'Source export without Git metadata; inventory follows repository ignore rules.')
    write_json(output/'manifest.json', report)
    return output.resolve()


def record_run(output, config_snapshot, profile, workload, backend, experiment_type, source_snapshot=None):
    output = Path(output)
    source = freeze_source(output/'source', (output,)) if source_snapshot is None else Path(source_snapshot)
    report = json.loads((source/'manifest.json').read_text())
    profile_data = json.loads(Path(profile).read_text())
    provenance = profile_data['provenance']
    config = json.loads(Path(config_snapshot).read_text())
    identity = dict(schema_version=1, experiment_type=experiment_type,
        input_sha256=dict(config=sha256(config_snapshot), profile=sha256(profile), workload=sha256(workload)),
        source=dict(git_head=report['git_head'], source_tree_sha256=report['source_tree_sha256'],
            archive_sha256=report['archive_sha256'], manifest=os.path.relpath(source/'manifest.json', output),
            archive=os.path.relpath(source/'source.tar.gz', output)),
        backend=backend, profile_status=profile_kind(provenance),
        profile_synthetic=provenance['synthetic'],
        execution_schema='pd_profile_driven_v1',
        parallel_semantics=dict(ep_size_unit=('GPU_ranks' if config.get('expert_parallel',{}).get('rank_unit') == 'gpu' else 'TP_teams'),
            ep_execution='asynchronous_source_batch_dispatch_expert_fifo_combine',
            decode_execution='aggregate_dense_step_then_MoE_layers') if config.get('expert_parallel',{}).get('enabled') else None,
        semantics='Feedback affects future compute/communication submissions.' if experiment_type=='end_to_end'
            else 'Submission times frozen; transport attribution only, not request/SLO improvement.')
    ep = profile_data.get('expert_parallel',{})
    if identity['parallel_semantics'] is not None and 'layers' in profile_data['phases']['decode']:
        identity['parallel_semantics']['decode_execution']='layerwise_nonexpert_compute_then_corresponding_MoE'
    if identity['parallel_semantics'] is not None and ('routing_mode' in ep or 'communication_mode' in ep):
        identity['parallel_semantics'].update(
            ep_routing=ep.get('routing_mode','shared_periodic'),ep_routing_seed=ep.get('routing_seed',0),
            ep_communication=ep.get('communication_mode','expert_assignment'),
            local_combine_seconds_per_extra_assignment=ep.get('local_combine_seconds_per_extra_assignment',0.))
    if (output/'binding_plan.json').is_file():
        identity['input_sha256']['binding_plan'] = sha256(output/'binding_plan.json')
    if config.get('expert_parallel',{}).get('enabled') and config['expert_parallel'].get('rank_unit') == 'gpu':
        placements={p['instance_id']:p for p in config['topology']['placements']}
        identity['parallel_semantics']['phases']={role:dict(attention_tp=1,
            attention_dp_instances=sum(p['role']==role for p in placements.values()),
            expert_ep_size=next(len(g) for g in config['expert_parallel']['groups'] if placements[g[0]]['role']==role),
            model_units=sum(placements[g[0]]['role']==role for g in config['expert_parallel']['groups']),
            weights_bytes_per_gpu=profile_data.get('weights_bytes_per_gpu_by_phase',{}).get(role,profile_data['weights_bytes_per_gpu']))
            for role in ('prefill','decode')}
    write_json(output/'reproducibility.json', identity)
    return identity


def write_suite_report(output, report):
    """A short human-readable index; numeric authority remains summary.json."""
    output=Path(output)
    lines=['# 端到端策略对照', '', '类型：`end_to_end`。网络反馈会影响后续执行。', '',
           f"成功 {report['successful_runs']} / {report['total_runs']} 次，失败 {report['failed_runs']} 次。", '']
    execution = report.get('execution')
    if execution:
        mode = '多进程' if execution['mode'] == 'multiprocess' else '串行'
        lines.extend([f"执行方式：{mode}，独立实验并发上限 {execution['max_workers']}（请求 {execution['requested_jobs']}）。", ''])
    lines.extend(['| case | 成功 | SLO goodput 均值（请求/秒） |', '| --- | ---: | ---: |'])
    for case in report['cases']:
        value=case['metrics']['slo_goodput_rps']['mean']
        lines.append(f"| {case['case_id']} | {case['successful_runs']} | {value if value is not None else '无有效样本'} |")
    lines.extend(['', '统计口径和失败样本见 [summary.json](summary.json)，各次输入、模型估算状态、源码及后端身份见 `runs/*/run/reproducibility.json`。',
                  '', '短时间片段只解释对应仿真场景；profile 的估算类型和建模假设随输入保留。'])
    (output/'report.md').write_text('\n'.join(lines)+'\n')
