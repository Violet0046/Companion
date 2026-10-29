"""Run every existing script test in an isolated process (no model API calls)."""
from pathlib import Path
import os
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    failed = []
    tests = sorted((root / 'tests').glob('test_*.py'))
    for test in tests:
        try:
            result = subprocess.run(
                [sys.executable, str(test)], cwd=root,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                capture_output=True, text=True, encoding='utf-8', timeout=60,
            )
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
