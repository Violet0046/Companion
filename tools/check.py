"""Run every existing script test in an isolated process (no model API calls)."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile


def run_script(root,test):
    with tempfile.TemporaryDirectory(prefix='companion-test-') as tmp:
        return subprocess.run([sys.executable,str(test)],cwd=root,
            env={**os.environ,'PYTHONIOENCODING':'utf-8',
                 'COMPANION_DB_PATH':str(Path(tmp)/'fallback.db'),
                 'OPENAI_API_KEY':'test-no-network','OPENAI_BASE_URL':'http://127.0.0.1:9/v1',
                 'COMPANION_LLM_MODEL':'test-no-network','COMPANION_AUTO_MEMORY':'0'},
            capture_output=True,text=True,encoding='utf-8',timeout=60)


def main():
    root = Path(__file__).resolve().parents[1]
    failed = []
    tests = sorted((root / 'tests').glob('test_*.py'))
    for test in tests:
        try:
            result = run_script(root,test)
            ok = result.returncode == 0
            print(f'{test.name}: {"PASS" if ok else "FAIL"}', flush=True)
            if not ok:
                print(result.stdout + result.stderr)
                failed.append(test.name)
        except subprocess.TimeoutExpired:
            print(f'{test.name}: TIMEOUT', flush=True)
            failed.append(test.name)
    print(f'{len(tests) - len(failed)}/{len(tests)} script suites passed')
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
