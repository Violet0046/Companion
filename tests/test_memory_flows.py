"""Cross-turn contracts, genuine failure reporting and expectation isolation."""
import copy
import json
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from companion_ever.config import AppConfig
from evaluation.flows import run_flow_suite,ReferenceModel


if __name__=='__main__':
    suite=json.loads((Path(__file__).parent/'fixtures/memory_flows.json').read_text(encoding='utf-8'))
    suite['never_send']='FLOW_EVALUATION_ONLY_DO_NOT_SEND'
    original=ReferenceModel.chat
    def observed(self,messages,**kwargs):
        assert suite['never_send'] not in json.dumps(messages,ensure_ascii=False)
        return original(self,messages,**kwargs)
    with tempfile.TemporaryDirectory() as tmp:
        cfg=AppConfig(Path(tmp)/'unused.db')
        with patch.object(ReferenceModel,'chat',observed):
            result=run_flow_suite(suite,cfg,Path(tmp)/'reference.json',reference=True)
        assert result['passed']==result['total']==6
        assert result['semantic_review_status']=='pending'
        for row in result['cases']:
            assert row['integrity']=='ok' and not row['foreign_key_errors']
            for metrics in row['usage_by_layer'].values():
                assert metrics['input_tokens'] is None and metrics['cache_hit_ratio'] is None
        bad=copy.deepcopy(suite);bad['flows']=bad['flows'][:1]
        bad['flows'][0]['steps'][1]['checks'][0]['equals']=999
        result=run_flow_suite(bad,cfg,Path(tmp)/'bad.json',reference=True)
        assert result['passed']==0 and result['cases'][0]['status']=='contract_failed'
    print('[OK] six production flow contracts, scoped checks, explicit unknown costs and expectation isolation')
