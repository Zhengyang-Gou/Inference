"""Gate-group limits, balanced marginals and independent cursor conservation."""
import copy
import unittest
from collections import Counter
from itertools import combinations

from joint_scheduling.backend.build import PACKAGE
from joint_scheduling.backend.compute_profile import ComputeProfile
from joint_scheduling.backend.expert_profile import ExpertProfile
from joint_scheduling.backend.expert_routing import balanced_group_pattern


class GroupedRoutingTests(unittest.TestCase):
    def profile(self):
        return ComputeProfile(PACKAGE/'profiles/deepseek_v3_h800_tp1_ep32_ep128_estimated.json').expert

    def test_all_group_subsets_and_expert_marginals_are_balanced(self):
        pattern = balanced_group_pattern(256,8,8,4)
        selected_groups = Counter()
        experts = Counter()
        for row in pattern:
            self.assertEqual(len(row),8)
            self.assertEqual(len(set(row)),8)
            groups = Counter(expert//32 for expert in row)
            self.assertEqual(sorted(groups.values()),[2,2,2,2])
            selected_groups[tuple(sorted(groups))] += 1
            experts.update(row)
        self.assertEqual(set(selected_groups),set(combinations(range(8),4)))
        self.assertEqual(set(selected_groups.values()),{32})
        self.assertEqual(set(experts),set(range(256)))
        self.assertEqual(set(experts.values()),{70})
        self.assertEqual(pattern,balanced_group_pattern(256,8,8,4))
        for tokens in (2,8,32,70,374,512):
            prefix=Counter(expert//32 for row in pattern[:tokens] for expert in row)
            self.assertEqual(set(prefix.values()),{tokens})

    def test_cached_period_counts_match_brute_force_and_huge_cursors(self):
        expert = self.profile();pattern=expert.raw['routing_pattern'];period=len(pattern)
        for tokens,offset in ((0,0),(1,0),(8,31),(512,2238),(period+7,period-1),(2*period+1,17)):
            expected = [0]*256
            for token in range(offset,offset+tokens):
                for target in pattern[token%period]:expected[target] += 1
            self.assertEqual(expert.counts(tokens,offset),expected)
        rounds=10**8
        expected=[70*rounds]*256
        for row in pattern[-2:]+pattern[:3]:
            for target in row:expected[target] += 1
        self.assertEqual(expert.counts(rounds*period+5,period-2),expected)
        total=expert.counts(14050,31)
        chunks=[expert.counts(min(512,14050-start),31+start) for start in range(0,14050,512)]
        self.assertEqual(total,[sum(chunk[i] for chunk in chunks) for i in range(256)])

    def test_declared_gate_rejects_missing_invalid_or_violating_constraints(self):
        base=self.profile().raw
        for change in ({'n_group':3},{'topk_group':9},{'n_group':True},
                       {'routing_pattern':[[0,32,64,96,128,160,192,224]]}):
            raw=copy.deepcopy(base);raw.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):ExpertProfile(raw,61)
        raw=copy.deepcopy(base);raw.pop('topk_group')
        with self.assertRaisesRegex(ValueError,'together'):ExpertProfile(raw,61)
        # Old profiles remain readable without a declared grouping contract.
        raw.pop('n_group')
        raw['routing_pattern']=[[0,32,64,96,128,160,192,224]]
        self.assertEqual(sum(ExpertProfile(raw,61).counts(1)),8)

    def test_balanced_builder_rejects_infeasible_group_sizes(self):
        for args in ((256,8,3,2),(256,8,8,9),(256,7,8,4),(4,8,2,2),(256,8,True,1)):
            with self.subTest(args=args),self.assertRaises(ValueError):balanced_group_pattern(*args)


if __name__=='__main__':unittest.main()
