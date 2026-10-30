"""Fake model helpers for the production Draft contract; never call a provider."""
import json

def no_memory(messages, **kwargs):
    case = json.loads(messages[-1]['content'])
    new = set(case['new_message_ids'])
    return json.dumps({'proposals':[],'links':[],'uncertainties':[],
                      'skipped':[{'message_id':m['id'],'reason':'本批没有长期信息'}
                                 for m in case['messages'] if m['id'] in new and m['role']=='user']})
