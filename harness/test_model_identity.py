"""Identity regressions: tag/config identity never substitutes for weight identity."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from context_manager import InvalidUpdate
import model_identity as m


def identity(name, weight=None, manifest=None, family=None):
    return dict(m.unknown(name), weights_sha256=weight, manifest_digest=manifest, family=family)


class IdentityTests(unittest.TestCase):
    def test_alias_templates_share_weights_but_adapters_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'blobs').mkdir()
            layer=lambda kind,d: {'mediaType':'application/vnd.ollama.image.'+kind,'digest':'sha256:'+d*64,'size':10}
            def save(layers):
                raw=json.dumps({'layers':layers}).encode();digest=hashlib.sha256(raw).hexdigest()
                (root/'blobs'/('sha256-'+digest)).write_bytes(raw)
                return digest
            a=save([layer('model','a'),layer('template','b')])
            b=save([layer('model','a'),layer('template','c')])
            c=save([layer('model','a'),layer('adapter','d')])
            self.assertNotEqual(a,b);self.assertEqual(m.weights(a,root),m.weights(b,root))
            self.assertNotEqual(m.weights(a,root),m.weights(c,root))
            (root/'blobs'/('sha256-'+a)).write_bytes(b'{}')
            self.assertIsNone(m.weights(a,root))
            self.assertIsNone(m.weights('../escape',root))
    def test_tensor_alias_provenance_and_order_do_not_change_weight_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'blobs').mkdir()
            layers=[{'mediaType':'application/vnd.ollama.image.tensor','digest':'sha256:'+c*64,'name':n} for c,n in [('a','input'),('b','output')]]
            def save(items):
                raw=json.dumps({'layers':items}).encode();digest=hashlib.sha256(raw).hexdigest()
                (root/'blobs'/('sha256-'+digest)).write_bytes(raw);return m.weights(digest,root)
            original=save(layers)
            alias=save([dict(x,**{'from':'base:latest'}) for x in reversed(layers)])
            self.assertEqual(original,alias)
            layers[0]['name']='changed-role';self.assertNotEqual(original,save(layers))

    def test_unknown_digests_do_not_prove_distinct_weights(self):
        a=identity('writer',manifest='a');b=identity('reader',manifest='b')
        result=m.compare(a,b);self.assertEqual(result['weight_relation'],'unknown')
        with self.assertRaises(InvalidUpdate):m.enforce(result,True)
        b['manifest_digest']='a';self.assertEqual(m.compare(a,b)['weight_relation'],'same')
    def test_explicit_alias_is_replaced_and_smallest_separated_family_preferred(self):
        writer=identity('writer','same','a','qwen')
        rows=[{'name':n,'parameter_count':size,'identity':identity(n,w,n,f)} for n,size,w,f in
              [('alias',1,'same','qwen'),('small',4,'other','qwen'),('different',8,'third','llama')]]
        name,result=m.choose(writer,'alias',rows)
        self.assertEqual(name,'different');self.assertEqual(result['selection'],'replaced_unseparated_choice')
        name,result=m.choose(writer,'small',rows)
        self.assertEqual(name,'small');self.assertFalse(result['statistical_independence_verified'])
        rows[-1]['eligible']=False
        self.assertEqual(m.choose(writer,'auto',rows)[0],'small')
        with self.assertRaises(InvalidUpdate):m.choose(writer,'different',rows)
    def test_fallback_and_unknown_are_labelled_not_called_independent(self):
        writer=identity('writer','a')
        _,sep=m.choose(writer,models=[{'name':'writer','identity':writer}])
        self.assertEqual(sep['weight_relation'],'same');self.assertIn('Same model weights',m.label(sep))
        _,sep=m.choose(m.unknown('writer'),models=[{'name':'reader'}])
        self.assertEqual(sep['weight_relation'],'unknown');self.assertIn('unknown',m.label(sep))
    def test_show_metadata_supplies_family_when_tags_are_empty(self):
        with patch.object(m,'weights',return_value='weight'):
            result=m.from_metadata('model',{'digest':'d','details':{}},{'model_info':{'general.architecture':'qwen3_5_moe'},'details':{'runner':'mlx'}})
        self.assertEqual(result['family'],'qwen');self.assertEqual(result['backend'],'mlx')
        self.assertIsNone(m.family('unfamiliar_engine'))
    def test_recorded_identity_survives_name_retagging(self):
        original=identity('writer','a','old')
        retagged=identity('writer','b','new')
        rows=[{'name':'writer','identity':retagged}]
        self.assertEqual(m.choose(original,models=rows)[1]['weight_relation'],'different')
        self.assertEqual(original['weights_sha256'],'a')
