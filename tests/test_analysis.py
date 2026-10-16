import unittest
from pathlib import Path
from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph

FIXTURE = Path(__file__).parent / 'fixtures' / 'mini'

class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ast_data = scan_repository(FIXTURE)
        cls.symbols = build_symbol_result(cls.ast_data)
        cls.graph = build_graph(cls.symbols)

    def relations(self, caller):
        byid = {s['id']: s['canonical_name'] for s in self.symbols['symbols']}
        return [(byid.get(e['source']), byid.get(e['target']), e['type'], e['evidence'].get('rule'))
                for e in self.graph['edges'] if byid.get(e['source'], '').endswith(caller)]

    def has_edge(self, caller, target, typ='CALLS'):
        return any(t == typ and s.endswith(caller) and d.endswith(target) for s, d, t, rule in self.relations(caller))

    def test_parser_diagnostics(self):
        self.assertEqual(len(self.ast_data['parse_errors']), 1)
        self.assertTrue(self.ast_data['parse_errors'][0]['file'].endswith('bad_syntax.py'))

    def test_inherited_and_super(self):
        self.assertTrue(self.has_edge('GoogleEnum.extract_domains', 'Base.print_'))
        self.assertTrue(self.has_edge('GoogleEnum.extract_domains', 'Base.__init__'))

    def test_constructor_and_instance(self):
        self.assertTrue(self.has_edge('launch', 'GoogleEnum', 'INSTANTIATES'))
        self.assertTrue(self.has_edge('launch', 'Base.__init__'))
        self.assertTrue(self.has_edge('launch', 'GoogleEnum.extract_domains'))

    def test_imported_package_function(self):
        self.assertTrue(self.has_edge('launch', 'subbrute.subbrute.print_target'))
        self.assertTrue(self.has_edge('launch', 'subbrute.subbrute.trace'))

    def test_scope_separation(self):
        self.assertTrue(self.has_edge('foo', 'Payment.run'))
        self.assertTrue(self.has_edge('bar', 'Other.run'))
        self.assertFalse(self.has_edge('foo', 'Other.run'))
        self.assertFalse(self.has_edge('bar', 'Payment.run'))

    def test_reassignment_order(self):
        e = [e for e in self.graph['edges'] if e['source'].endswith('reassigned') and e['type'] == 'CALLS']
        calls = [e for e in e if (e.get('evidence') or {}).get('source') == 'svc.run()']
        targets = [x['target'] for x in sorted(calls, key=lambda x: x['evidence']['line'])]
        self.assertEqual(targets, ['METHOD:sublist3r.Payment.run', 'METHOD:sublist3r.Other.run'])

    def test_branch_merged_candidates(self):
        # Both alternatives are reachable. Emit MAY-call edges, never pick one.
        self.assertTrue(self.has_edge('branch', 'Payment.run'))
        self.assertTrue(self.has_edge('branch', 'Other.run'))
        self.assertTrue(any(c['callee_text'] == 'svc.run' and len(c['targets']) == 2
                            for c in self.graph['ambiguous_calls']))

    def test_same_branch_assignment(self):
        self.assertTrue(self.has_edge('same_branch', 'Payment.run'))
        self.assertFalse(self.has_edge('same_branch', 'Other.run'))

    def test_conditional_definition_outside_branch(self):
        self.assertTrue(self.has_edge('conditional_outside', 'Payment.run'))
        self.assertTrue(any(c['callee_text'] == 'pscan.run' and c['reason'] == 'may_be_unbound_on_some_paths'
                            for c in self.graph['conditional_calls']))
        self.assertTrue(any(e.get('evidence', {}).get('certainty') == 'conditional_may_call'
                            for e in self.graph['edges'] if e['source'].endswith('conditional_outside')
                            and e['target'].endswith('Payment.run')))

    def test_both_branches_inside(self):
        self.assertTrue(self.has_edge('branch_both_inside', 'Payment.run'))
        self.assertTrue(self.has_edge('branch_both_inside', 'Other.run'))
        calls = [e for e in self.graph['edges'] if e['source'].endswith('branch_both_inside')
                 and e.get('evidence', {}).get('source') == 'svc.run()']
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(e['evidence'].get('certainty') == 'definite_within_supported_flow'
                            for e in calls))

    def test_preexisting_value_reaches_if_join(self):
        self.assertTrue(self.has_edge('preexisting_and_branch', 'Payment.run'))
        self.assertTrue(self.has_edge('preexisting_and_branch', 'Other.run'))

    def test_reassigned_after_join(self):
        self.assertTrue(self.has_edge('branch_then_override', 'Payment.run'))
        self.assertFalse(self.has_edge('branch_then_override', 'Other.run'))

    def test_alias_captures_old_value(self):
        self.assertTrue(self.has_edge('alias_not_updated', 'Payment.run'))
        self.assertFalse(self.has_edge('alias_not_updated', 'Other.run'))

    def test_only_reachable_branch_after_return(self):
        self.assertTrue(self.has_edge('return_branch', 'Other.run'))
        self.assertFalse(self.has_edge('return_branch', 'Payment.run'))

    def test_unknown_overwrite_kills_previous_type(self):
        self.assertFalse(self.has_edge('unknown_overwrites', 'Payment.run'))
        self.assertFalse(self.has_edge('unknown_overwrites', 'Other.run'))

    def test_partial_unknown_branch_is_not_definite(self):
        self.assertTrue(self.has_edge('conditional_unknown', 'Payment.run'))
        self.assertTrue(any(e.get('evidence', {}).get('certainty') == 'conditional_may_call'
                            for e in self.graph['edges'] if e['source'].endswith('conditional_unknown')
                            and e['target'].endswith('Payment.run')))

    def test_no_import_no_cross_module_guess(self):
        self.assertFalse(self.has_edge('caller', 'sublist3r.launch'))

    def test_for_loop_class_candidates(self):
        self.assertTrue(self.has_edge('dispatch', 'GoogleEnum', 'INSTANTIATES'))
        self.assertTrue(self.has_edge('dispatch', 'AskEnum', 'INSTANTIATES'))

    def test_package_dotted_import_without_alias(self):
        self.assertTrue(self.has_edge('called_via_package', 'subbrute.subbrute.print_target'))

    def test_param_shadowing(self):
        self.assertFalse(self.has_edge('shadowed_parameter', 'sublist3r.launch'))

    def test_nested_function(self):
        self.assertTrue(self.has_edge('outer.inner', 'outer.nested'))

    def test_evidence(self):
        self.assertTrue(any('rule' in x['evidence'] and 'call_id' in x['evidence']
                            for x in self.graph['edges'] if x['type'] == 'CALLS'))

if __name__ == '__main__':
    unittest.main()
