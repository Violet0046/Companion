"""时间分隔条规则的回归测试

规则（两个条件满足其一就插一条）：
1. 时间间隔 > 10 分钟
2. 连续超过 10 条消息

为什么两个条件：
- 只看时间：AI 陪聊时正常间隔就几秒，按分钟切会把连贯的聊天切碎；
  微信那套 5 分钟对"陪聊"太勤。
- 只看条数：停了很久才回一句话时，中间会一大段没头没尾。
两个阈值配套（10 条 ≈ 10 分钟），切出来的块大小才一致。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def extract_fn(src: str, name: str) -> str:
    """从 JS 源码里切出一个函数（花括号配平）。"""
    idx = src.find("function " + name + "(")
    assert idx >= 0, f"app.js 里找不到 {name}"
    i = src.index("{", idx)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[idx:j + 1]
    raise AssertionError(f"{name} 花括号没配平")


HARNESS = r"""
const MIN = 10, MAXG = 10;
const GAP = MIN * 60 * 1000;
function needTimeSep(prevIso, curIso, sinceLastSep) {
  if (sinceLastSep >= MAXG) return true;
  if (!prevIso || !curIso) return false;
  const a = new Date(prevIso), b = new Date(curIso);
  if (isNaN(a) || isNaN(b)) return false;
  return Math.abs(b.getTime() - a.getTime()) > GAP;
}
function fmtTimeSep(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const now = new Date();
  const p = (n) => String(n).padStart(2, '0');
  const time = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const dayStart = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((dayStart(now) - dayStart(d)) / 86400000);
  if (days <= 0) return time;
  if (days === 1) return `昨天 ${time}`;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日 ${time}`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${time}`;
}

const base = new Date('2026-10-28T22:00:00');
const at = (min) => new Date(base.getTime() + min * 60000).toISOString();
const cases = [
  // [说明, 上一条, 这一条, 距上一条分隔条几条, 期望是否插]
  ['隔 5 分钟，不插',        at(0),  at(5),   0,  false],
  ['隔 11 分钟，插',         at(0),  at(11),  0,  true],
  ['隔 1 小时，插',          at(0),  at(60),  0,  true],
  ['只隔 1 分钟但已 10 条，插', at(0),  at(1),  10, true],
  ['只隔 1 分钟但才 9 条，不插', at(0),  at(1),  9,  false],
  ['隔 9 分钟且 10 条，插',   at(0),  at(9),  10, true],
];
let fail = 0;
for (const [desc, a, b, since, want] of cases) {
  const got = needTimeSep(a, b, since);
  if (got !== want) {
    console.error(`FAIL ${desc}: got=${got} want=${want}`);
    fail++;
  }
}
// 文案
const now = new Date();
const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 22, 25).toISOString();
const yest = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 22, 25).toISOString();
if (fmtTimeSep(today) !== '22:25') { console.error('FAIL 今天 -> ' + fmtTimeSep(today)); fail++; }
if (fmtTimeSep(yest) !== '昨天 22:25') { console.error('FAIL 昨天 -> ' + fmtTimeSep(yest)); fail++; }
if (fail) { console.error(fail + ' 个用例失败'); process.exit(1); }
console.log('[OK] ' + cases.length + ' 个分隔条用例全过');
"""


def main() -> int:
    from companion_ever.entrypoints.web.server import WEB_DIR

    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # 源码里必须真的用了这套规则
    assert "function needTimeSep" in js, "缺少 needTimeSep"
    assert "TIME_SEP_GAP_MIN = 10" in js, "时间阈值应该是 10 分钟"
    assert "TIME_SEP_MAX_MSGS = 10" in js, "条数阈值应该是 10"
    assert "function fmtTimeSep" in js, "缺少 fmtTimeSep"
    # 气泡内不再有时间戳
    assert "'stamp'" not in js and 'class="stamp"' not in js, \
        "气泡内不该再有时间戳（改用时间分隔条）"
    assert "time-sep" in js, "没有用到时间分隔条"
    print("[OK] 源码用的是「10 分钟 / 10 条」规则，且气泡内无时间戳")

    node = shutil.which("node")
    if not node:
        print("[SKIP] 环境里没有 node，跳过逻辑测试")
        return 0

    snippet = "\n\n".join([
        extract_fn(js, "needTimeSep"),
        extract_fn(js, "fmtTimeSep"),
        HARNESS,
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(snippet)
        path = f.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if r.returncode != 0:
        raise AssertionError(f"分隔条用例失败:\n{r.stdout}\n{r.stderr}")
    print(r.stdout.strip())

    print("[ALL] 时间分隔条测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())