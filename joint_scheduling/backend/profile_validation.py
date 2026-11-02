"""Grouped holdout validation of public operator interpolation; not engine validation."""
import csv
import hashlib
import math
import statistics
from collections import defaultdict
from .public_profile import SHAPE, MLP_OPS, ATTENTION_OPS, _at


def matching_rows(mlp_path, attention_path, tp_size=4):
    def read(path):
        with open(path,newline='') as handle:
            return list(csv.DictReader(handle))
    mlp=[r for r in read(mlp_path) if int(r['num_tensor_parallel_workers'])==tp_size
         and int(r['n_embd'])==SHAPE['hidden_size'] and int(r['n_head'])==SHAPE['num_attention_heads']
         and int(r['n_kv_head'])==SHAPE['num_key_value_heads']
         and int(r['n_expanded_embd'])==SHAPE['intermediate_size']
         and int(r['vocab_size'])==SHAPE['vocab_size'] and r['use_gated_mlp']=='True']
    attention=[r for r in read(attention_path) if int(r['num_tensor_parallel_workers'])==tp_size
         and int(r['n_embd'])==SHAPE['hidden_size'] and int(r['n_q_head'])==SHAPE['num_attention_heads']
         and int(r['n_kv_head'])==SHAPE['num_key_value_heads'] and int(r['block_size'])==16
         and r['attention_backend']=='AttentionBackend.FLASHINFER']
    if not mlp or not attention:
        raise ValueError("no matching public operator measurements")
    return mlp,attention


def aggregate_curve(rows, coordinate, operations):
    grouped=defaultdict(list)
    for row in rows:
        values=[float(row['time_stats.'+op+'.median']) for op in operations]
        if any(not math.isfinite(v) or v<0 for v in values):
            raise ValueError('invalid measured operator time')
        grouped[int(row[coordinate])].append(sum(values))
    return {key:statistics.median(values) for key,values in sorted(grouped.items())}


def holdout_curve(curve, family, group, folds=5):
    """Endpoints stay in training. All repeats at one coordinate are held out together."""
    if type(folds) is not int or folds<2:
        raise ValueError('holdout folds must be >=2')
    keys=sorted(curve)
    if len(keys)<3:return []
    output=[]
    for fold in range(folds):
        held={key for index,key in enumerate(keys[1:-1]) if index%folds==fold}
        if not held:continue
        trained={key:value for key,value in curve.items() if key not in held}
        assert not held & trained.keys()
        for key in sorted(held):
            actual=curve[key];predicted=_at(trained,key)
            absolute=abs(predicted-actual)
            output.append(dict(family=family,group=str(group),fold=fold,coordinate=key,
                observed_ms=actual,predicted_ms=predicted,absolute_error_ms=absolute,
                signed_error_pct=(predicted-actual)/actual*100 if actual else None,
                absolute_error_pct=absolute/actual*100 if actual else None,
                training_coordinates=len(trained),heldout_coordinates=len(held),
                training_coordinate_sha256=hashlib.sha256(','.join(map(str,sorted(trained))).encode()).hexdigest()))
    return output


def percentile(values, fraction):
    values=sorted(values)
    if not values:return None
    position=(len(values)-1)*fraction;lo=int(position);hi=math.ceil(position)
    return values[lo]+(values[hi]-values[lo])*(position-lo)


def validate_public_operators(mlp_path, attention_path, tp_size=4, folds=5):
    mlp,attention=matching_rows(mlp_path,attention_path,tp_size)
    families=defaultdict(dict)
    families['mlp_query_tokens']['all']=aggregate_curve(mlp,'num_tokens',MLP_OPS)
    families['embedding_query_tokens']['all']=aggregate_curve(mlp,'num_tokens',('emb',))
    families['prefill_zero_context']['batch1']=aggregate_curve([r for r in attention
        if r['is_prefill']=='True' and int(r['batch_size'])==1 and int(r['kv_cache_size'])==0],
        'prefill_chunk_size',ATTENTION_OPS)
    decodes=[r for r in attention if r['is_prefill']=='False' and int(r['prefill_chunk_size'])==0]
    prefills=[r for r in attention if r['is_prefill']=='True' and int(r['batch_size'])==1]
    for batch in sorted({int(r['batch_size']) for r in decodes}):
        families['decode_context'][batch]=aggregate_curve([r for r in decodes if int(r['batch_size'])==batch],
            'kv_cache_size',ATTENTION_OPS)
    for context in sorted({int(r['kv_cache_size']) for r in decodes}):
        families['decode_batch'][context]=aggregate_curve([r for r in decodes if int(r['kv_cache_size'])==context],
            'batch_size',ATTENTION_OPS)
    for query in sorted({int(r['prefill_chunk_size']) for r in prefills}):
        families['prefill_cached_context'][query]=aggregate_curve([r for r in prefills if int(r['prefill_chunk_size'])==query],
            'kv_cache_size',ATTENTION_OPS)
    for context in sorted({int(r['kv_cache_size']) for r in prefills if int(r['kv_cache_size'])>0}):
        families['prefill_query_at_context'][context]=aggregate_curve([r for r in prefills if int(r['kv_cache_size'])==context],
            'prefill_chunk_size',ATTENTION_OPS)
    records=[];summary=[]
    for family,groups in families.items():
        rows=[row for group,curve in groups.items() for row in holdout_curve(curve,family,group,folds)]
        records.extend(rows);errors=[r['absolute_error_pct'] for r in rows if r['absolute_error_pct'] is not None]
        summary.append(dict(family=family,groups=len(groups),evaluable_groups=sum(len(c)>=3 for c in groups.values()),
            heldout_points=len(rows),defined_relative_error_points=len(errors),zero_observed_points=sum(r["observed_ms"]==0 for r in rows),mape_pct=statistics.mean(errors) if errors else None,
            median_ape_pct=percentile(errors,.5),p95_ape_pct=percentile(errors,.95),max_ape_pct=max(errors) if errors else None,
            mean_absolute_error_ms=statistics.mean(r['absolute_error_ms'] for r in rows) if rows else None))
    return records,dict(method='{}-fold interior coordinate holdout; duplicate-coordinate medians grouped before splitting; endpoints always retained'.format(folds),
        folds=folds,training_holdout_coordinates_disjoint=True,source_independence=False,end_to_end_accuracy_validated=False,
        below_minimum_attention_anchor_validated=False,families=summary,
        interpretation='Tests in-range operator interpolation, not 2D whole-batch or full-engine accuracy; no global latency correction is fitted.')


def main():
    import argparse,json
    from pathlib import Path
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mlp',type=Path,required=True)
    parser.add_argument('--attention',type=Path,required=True)
    parser.add_argument('--tp-size',type=int,default=4)
    parser.add_argument('--folds',type=int,default=5)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    records,summary=validate_public_operators(args.mlp,args.attention,args.tp_size,args.folds)
    summary['inputs']={name:dict(path=str(path.resolve()),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                       for name,path in (('mlp',args.mlp),('attention',args.attention))}
    joint_records,joint_summary=validate_composed_grid(args.mlp,args.attention,args.tp_size,args.folds)
    args.output.mkdir(parents=True,exist_ok=False)
    for name,rows in (('holdout_predictions.csv',records),('holdout_summary.csv',summary['families']),('composed_grid_predictions.csv',joint_records)):
        with (args.output/name).open('x',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=list(rows[0]) if rows else [])
            writer.writeheader();writer.writerows(rows)
    (args.output/'holdout_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    (args.output/'composed_grid_summary.json').write_text(json.dumps(joint_summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))



def joint_coordinate_holdout(curves,linear,embedding,family,folds=5,layers=80):
    """Hold whole query and history coordinates out jointly, including query MLP data.

    Reference values are compositions of measured operators, not measured whole
    forwards. Unsupported boundary combinations are counted rather than extrapolated.
    """
    sizes=sorted(curves);contexts=sorted({c for curve in curves.values() for c in curve})
    if len(sizes)<3 or len(contexts)<3:
        return [],dict(family=family,evaluated_points=0,eligible_points=0,skipped_points=0,reason='insufficient grid')
    records=[];skips=defaultdict(int);eligible=0
    for size_fold in range(folds):
        held_sizes={s for i,s in enumerate(sizes[1:-1]) if i%folds==size_fold}
        trained_sizes=[s for s in sizes if s not in held_sizes]
        trained_linear={q:v for q,v in linear.items() if q not in held_sizes}
        trained_embedding={q:v for q,v in embedding.items() if q not in held_sizes}
        for context_fold in range(folds):
            held_contexts={c for i,c in enumerate(contexts[1:-1]) if i%folds==context_fold}
            trained={s:{c:v for c,v in curves[s].items() if c not in held_contexts} for s in trained_sizes}
            for size in sorted(held_sizes):
                lo=max(s for s in trained_sizes if s<size);hi=min(s for s in trained_sizes if s>size)
                for context in sorted(set(curves[size]) & held_contexts):
                    eligible+=1
                    if size not in linear or size not in embedding:
                        skips['no_direct_linear_or_embedding_measurement']+=1;continue
                    try:
                        assert size not in trained and context not in trained[lo] and context not in trained[hi]
                        assert size not in trained_linear and size not in trained_embedding
                        a=_at(trained[lo],context);b=_at(trained[hi],context)
                        attention=a+(b-a)*(size-lo)/(hi-lo)
                        prediction=layers*(_at(trained_linear,size)+attention)+_at(trained_embedding,size)
                    except ValueError:
                        skips['outside_retained_measurement_coverage']+=1;continue
                    reference=layers*(linear[size]+curves[size][context])+embedding[size]
                    records.append(dict(family=family,query_tokens_or_batch=size,cached_context=context,
                        size_fold=size_fold,context_fold=context_fold,reference_composed_ms=reference,
                        predicted_composed_ms=prediction,absolute_error_ms=abs(prediction-reference),
                        absolute_error_pct=abs(prediction-reference)/reference*100 if reference else None,
                        heldout_axes_absent_from_training=True))
    errors=[r['absolute_error_pct'] for r in records if r['absolute_error_pct'] is not None]
    summary=dict(family=family,evaluated_points=len(records),eligible_points=eligible,skipped_points=sum(skips.values()),
        skip_reasons=dict(skips),mape_pct=statistics.mean(errors) if errors else None,
        p95_ape_pct=percentile(errors,.95),max_ape_pct=max(errors) if errors else None,
        end_to_end_measurements=False,reference_is_operator_composition=True)
    return records,summary


def validate_composed_grid(mlp_path,attention_path,tp_size=4,folds=5):
    mlp,attention=matching_rows(mlp_path,attention_path,tp_size)
    linear=aggregate_curve(mlp,'num_tokens',MLP_OPS);embedding=aggregate_curve(mlp,'num_tokens',('emb',))
    decodes=[r for r in attention if r['is_prefill']=='False' and int(r['prefill_chunk_size'])==0]
    prefills=[r for r in attention if r['is_prefill']=='True' and int(r['batch_size'])==1]
    families={
        'decode_batch_and_context':{b:aggregate_curve([r for r in decodes if int(r['batch_size'])==b],'kv_cache_size',ATTENTION_OPS)
                                    for b in sorted({int(r['batch_size']) for r in decodes})},
        'prefill_query_and_context':{q:aggregate_curve([r for r in prefills if int(r['prefill_chunk_size'])==q],'kv_cache_size',ATTENTION_OPS)
                                    for q in sorted({int(r['prefill_chunk_size']) for r in prefills if int(r['kv_cache_size'])>0})}}
    records=[];summaries=[]
    for family,curves in families.items():
        rows,summary=joint_coordinate_holdout(curves,linear,embedding,family,folds,SHAPE['num_layers'])
        records.extend(rows);summaries.append(summary)
    return records,dict(method='Cartesian{}x{} coordinate folds: each interior query/context pair held out once; query coordinates removed from MLP/embedding too; endpoints retained'.format(folds,folds),
        folds_per_axis=folds,source_independence=False,end_to_end_accuracy_validated=False,
        references='Sum of measured operator medians over80uniform layers plus one embedding; not measured whole forwards',families=summaries)


if __name__=='__main__':
    main()
