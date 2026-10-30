"""Validate before writing, with one targeted correction inside the same budget."""
import json
import time
from .llm_json import parse_json_response
from .task_errors import describe_error
from .task_runtime import checkpoint
from companion_ever.trace import current_trace


def checked_json(llm,messages,validate,*,repair_attempts=0,**kwargs):
    messages=list(messages)
    total=kwargs.get('total_timeout')
    deadline=time.monotonic()+total if total is not None else None
    for attempt in range(repair_attempts+1):
        if deadline is not None:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise TimeoutError('模型整理及纠错超过本次任务的总时限')
            kwargs={**kwargs,'total_timeout':remaining}
        checkpoint('model_request',request_number=attempt+1,input_chars=sum(len(m['content']) for m in messages))
        raw=llm.chat(messages,**kwargs)
        data=parse_json_response(raw)
        trace=current_trace()
        if trace:
            trace.artifact(f'validation-{attempt+1:02d}.parsed',json.dumps(data,ensure_ascii=False,indent=2))
        try:
            checkpoint('validate',request_number=attempt+1)
            result=validate(data)
            if trace:
                trace.step('validation.passed',request_number=attempt+1)
            return result
        except ValueError as exc:
            if trace:
                detail=('; '.join('.'.join(map(str,e['loc']))+': '+e['msg'] for e in exc.errors(include_input=False,include_url=False))
                        if hasattr(exc,'errors') else str(exc))
                trace.artifact(f'validation-{attempt+1:02d}.error',detail)
                trace.step('validation.failed',request_number=attempt+1,error=describe_error(exc))
            if attempt==repair_attempts:
                raise
            checkpoint('repair',error=describe_error(exc))
            if kwargs.get('background_thinking') is False:
                # The M3 adapter can use adaptive thinking for this one repair;
                # other models keep their own supported defaults. No extra call.
                kwargs={**kwargs,'background_thinking':True}
            feedback=('; '.join('.'.join(map(str,e['loc']))+': '+e['msg'] for e in exc.errors(include_input=False,include_url=False))
                      if hasattr(exc,'errors') else str(exc))
            previous=json.dumps(data,ensure_ascii=False) if data is not None else '上次输出不是有效 JSON。'
            messages.extend([
                {'role':'assistant','content':previous[:24000]},
                {'role':'user','content':'上次输出未通过校验：'+feedback[:6000]+
                 '\n请依据最初输入修正上述错误，重新返回完整 JSON。quote 必须是对应来源的连续逐字原文，不能改写、补字或省略；不能核实的结论请移除，并遵守最初的来源覆盖要求。不得放宽证据与现实情境约束。'},
            ])
