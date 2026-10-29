"""从本地样本生成审阅报告；--model 才调用已配置的模型 API。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from companion_ever.services.memory_draft import generate_draft, render_review, validate_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', nargs='+', help='仅运行这些样本 ID')
    parser.add_argument('--model', action='store_true', help='调用 .env 配置的模型，可能产生费用')
    args = parser.parse_args()
    cases = json.loads(args.input.read_text(encoding='utf-8'))['cases']
    if args.case:
        missing = set(args.case) - {c['id'] for c in cases}
        if missing:
            parser.error(f'未知样本：{sorted(missing)}')
        cases = [c for c in cases if c['id'] in args.case]
    if len({c['id'] for c in cases}) != len(cases):
        parser.error('样本 ID 重复')
    for case in cases:
        validate_case(case)
    drafts = {}
    failed = False
    if args.model:
        from companion_ever.config import AppConfig
        from companion_ever.llm.llm_client import create_llm_client
        cfg = AppConfig.from_env()
        cfg.llm = type(cfg.llm)(api_key=cfg.llm.api_key, base_url=cfg.llm.base_url,
                                model=cfg.llm.model, timeout=45)
        llm = create_llm_client(cfg.llm)
        class Recorder:
            response = None

            def chat(self, messages):
                self.response = llm.chat(messages)
                return self.response

        for case in cases:
            recorder = Recorder()
            try:
                drafts[case['id']] = generate_draft(case, recorder).model_dump()
                print(case['id'] + ': draft validated', flush=True)
            except Exception as exc:
                # 不打印 SDK 异常正文，避免把请求细节/认证信息写入终端。
                drafts[case['id']] = {'error_type': type(exc).__name__,
                                      'status': '生成或结构校验失败，没有可用草稿'}
                if recorder.response is not None:
                    drafts[case['id']]['raw_response'] = recorder.response
                    drafts[case['id']]['validation_error'] = str(exc)
                failed = True
                print(case['id'] + ': FAILED (' + type(exc).__name__ + ')', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_review(cases, drafts), encoding='utf-8')
    if args.model:
        args.output.with_suffix('.json').write_text(json.dumps({
            'model': cfg.llm.model, 'drafts': drafts,
            'notice': '结构校验不等于语义合格；需人工审阅',
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(args.output.resolve()))
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
