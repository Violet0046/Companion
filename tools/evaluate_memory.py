"""Freeze review cases, replay saved drafts, or explicitly evaluate a configured model."""
import argparse
import json
import sys
import time
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from companion_ever.memory.extraction.draft import generate_draft
from evaluation.memory import evaluate, fingerprint, prompt_fingerprint, render_report, validate_suite

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / 'tests/fixtures/memory_cases.json'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def freeze(source, output):
    # This importer targets the reviewed C01..C18 snapshot; rules have no private text.
    templates = {c['id']: c['evaluation'] for c in json.loads(DEFAULT_SUITE.read_text(encoding='utf-8'))['cases']}
    original = json.loads(source.read_text(encoding='utf-8'))
    cases = []
    for original_case in original['cases']:
        case = dict(original_case)
        reviewed = case.get('user_review')
        template = templates.get(case['id'])
        if reviewed and template and template['status'] != 'unreviewed':
            case['evaluation'] = {**template, 'scope': reviewed['scope']}
        else:
            case['evaluation'] = {'status': 'unreviewed', 'constraints': {}, 'scope': '尚无可执行的已确认规则'}
        cases.append(case)
    suite = {'version': 1, 'provenance': 'private_review', 'source_sha256': fingerprint(original), 'cases': cases}
    validate_suite(suite)
    if output.exists():
        raise ValueError('冻结文件已存在，请使用新的版本路径，不覆盖旧基线')
    write_json(output, suite)
    return suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--background',action='store_true',help='验证生产后台的流式/纠错/思考配置')
    parser.add_argument('--flow',action='store_true',help='临时库中运行跨轮记忆/摘要/召回契约；仅支持 --reference 或 --model')
    parser.add_argument('--input', type=Path, default=DEFAULT_SUITE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--freeze', action='store_true', help='冻结本地审阅案例，输出必须放在私人目录')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--responses', type=Path, help='离线复评已有模型输出')
    mode.add_argument('--model', action='store_true', help='向 .env 的模型服务发送选中案例并产生费用')
    mode.add_argument('--reference', action='store_true', help='仅验证公共示例草稿，不代表模型效果')
    parser.add_argument('--case', nargs='+')
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    if args.output.suffix != '.json':
        parser.error('output 必须为 .json 路径，同时会生成 .md 报告')
    destinations = {args.output.resolve(), args.output.with_suffix('.md').resolve(),
                    args.output.with_suffix('.responses.json').resolve(), args.output.with_suffix('.suite.json').resolve()}
    if args.input.resolve() in destinations or (args.responses and args.responses.resolve() in destinations):
        parser.error('输出路径不能覆盖输入案例或已有模型响应')
    if args.freeze:
        if args.flow:parser.error('--flow cannot freeze a single-draft suite')
        private_root = (ROOT / 'memory/reviews').resolve()
        if private_root not in args.output.resolve().parents:
            parser.error('私人冻结集必须写入被 Git 忽略的 memory/reviews/ 下')
        suite = freeze(args.input, args.output)
        print(f"Frozen {len(suite['cases'])} cases; sha256={fingerprint(suite)}")
        return 0
    if not (args.responses or args.model or args.reference):
        parser.error('选择 --responses、--model 或 --reference')
    if args.flow:
        if args.responses or args.freeze or not (args.reference or args.model):
            parser.error('--flow requires --reference or --model')
        from companion_ever.config import AppConfig
        from evaluation.flows import run_flow_suite
        source=ROOT/'tests/fixtures/memory_flows.json' if args.input==DEFAULT_SUITE else args.input
        if source.resolve() in destinations:parser.error('output cannot overwrite the flow suite')
        suite=json.loads(source.read_text(encoding='utf-8'))
        if suite.get('provenance')=='private_review' and (ROOT/'memory/reviews').resolve() not in args.output.resolve().parents:
            parser.error('private flow results must stay under memory/reviews')
        if args.case:
            if set(args.case)-{flow['id'] for flow in suite['flows']}:parser.error('unknown flow ID')
            suite={**suite,'flows':[flow for flow in suite['flows'] if flow['id'] in args.case]}
        cfg=AppConfig.from_env();cfg.extraction_timeout=args.timeout
        result=run_flow_suite(suite,cfg,args.output,reference=args.reference)
        print(f"Flow contracts: {result['passed']}/{result['total']} ({result['mode']})")
        return int(result['passed']!=result['total'])
    suite = json.loads(args.input.read_text(encoding='utf-8'))
    validate_suite(suite)
    if args.case:
        if set(args.case) - {c['id'] for c in suite['cases']}:
            parser.error('包含未知案例 ID')
        suite = {**suite, 'cases': [c for c in suite['cases'] if c['id'] in args.case]}
    if suite.get('provenance') == 'private_review' and (ROOT / 'memory/reviews').resolve() not in args.output.resolve().parents:
        parser.error('私人评测结果必须写入 memory/reviews/ 下')
    if args.responses:
        responses = json.loads(args.responses.read_text(encoding='utf-8'))
    else:
        responses = {'suite_sha256': fingerprint(suite),
                    'prompt_sha256': prompt_fingerprint(),
                    'drafts': {}, 'measurements': {}}
        write_json(args.output.with_suffix('.suite.json'), suite)
        if args.model:
            from companion_ever.config import AppConfig
            from companion_ever.infrastructure.llm.openai_client import OpenAICompatibleClient
            cfg = AppConfig.from_env()
            if not (cfg.llm.api_key and cfg.llm.base_url and cfg.llm.model):
                parser.error('模型配置不完整')
            if not 0 < args.timeout < float('inf'):
                parser.error('timeout 必须为有限正数')
            llm = OpenAICompatibleClient(cfg.llm.api_key, cfg.llm.base_url, cfg.llm.model, timeout=args.timeout)
            from companion_ever.infrastructure.llm.traced_client import TracedLLMClient
            from companion_ever.infrastructure.tracing import TraceStore,trace_scope
            llm=TracedLLMClient(llm)
            trace_store=TraceStore(args.output.with_suffix('.traces'))
            responses['trace_dir']=str(trace_store.base_dir)
            responses['model'] = cfg.llm.model
        else:
            responses['model'] = 'reference-fixture (not a model evaluation)'
        response_path = args.output.with_suffix('.responses.json')
        class Recorder:
            response = None
            calls = 0

            def chat(self, messages, **kwargs):
                self.calls+=1
                self.response = llm.chat(messages, **kwargs)
                return self.response

        for case in suite['cases']:
            started = time.perf_counter()
            recorder = Recorder()
            case_trace=None
            try:
                options=({'stream':True,'total_timeout':args.timeout*3,'repair_attempts':1,
                          'background_thinking':cfg.background.model_thinking} if args.model and args.background else {})
                with (trace_scope('memory_evaluation','evaluation','evaluation',store=trace_store,case_id=case['id'])
                      if args.model else nullcontext()) as case_trace:
                    responses['drafts'][case['id']] = (generate_draft(case, recorder, timeout=args.timeout, max_retries=0,**options).model_dump()
                                                      if args.model else case['reference_draft'])
                print(case['id'] + (': draft validated' if args.model else ': reference loaded'), flush=True)
            except Exception as exc:
                responses['drafts'][case['id']] = {'error_type': type(exc).__name__}
                if recorder.response is not None:
                    responses['drafts'][case['id']].update(raw_response=recorder.response, validation_error=str(exc))
                print(case['id'] + ': FAILED (' + type(exc).__name__ + ')', flush=True)
            measure = {'elapsed_seconds': round(time.perf_counter() - started, 3)}
            if args.model:
                from companion_ever.infrastructure.llm.traced_client import summarize_calls
                calls=getattr(case_trace,'calls',[]) or []
                measure.update(model_calls=recorder.calls,usage=summarize_calls(calls))
                if len(calls)==recorder.calls:
                    usage=measure['usage']
                    if usage['input_tokens_reported_calls']==len(calls):measure['prompt_tokens']=usage['input_tokens']
                    if usage['output_tokens_reported_calls']==len(calls):measure['completion_tokens']=usage['output_tokens']
            responses['measurements'][case['id']] = measure
            write_json(response_path, responses)  # Keep completed cases even if interrupted later.
    report = evaluate(suite, responses)
    write_json(args.output, report)
    args.output.with_suffix('.md').write_text(render_report(report), encoding='utf-8')
    print(f"Structure: {report['structure_valid']}/{report['case_count']}; confirmed rules: {report['rule_passed']}/{report['confirmed_rule_cases']}")
    return int(any(r['status'] in ('missing', 'invalid', 'error', 'rule_failed') for r in report['cases']))


if __name__ == '__main__':
    raise SystemExit(main())
