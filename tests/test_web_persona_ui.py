r"""新建角色对话框的前端逻辑测试

把 app.js 里的 templateValues / buildDefinition 抽出来，用 node 跑真实逻辑：

- 选模板 → 表单值：列表拍平成"、"，文本原样，模板没有的字段留空
- 表单值 → persona_basics：空值不写进去；列表按、分隔拆开并去空格
- 往返：模板 → 表单值 → 定义，应当还原成原模板（用户占位符由后端替换，前端不管）

环境里没有 node 时跳过功能部分，只做静态检查。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

HARNESS = r"""
const FIELDS = [
  {key: 'name', type: 'text'},
  {key: 'identity', type: 'text'},
  {key: 'personality', type: 'list'},
  {key: 'background', type: 'text'},
  {key: 'values', type: 'list'},
];
const TEMPLATE = {
  name: '柳如烟', identity: '热辣女友',
  personality: ['温柔', '热情', '活泼'],
  background: '18岁舞蹈系女大学生',
  values: ['真诚', '忠诚'],
};

let fail = 0;
function check(label, got, expect) {
  const a = JSON.stringify(got), b = JSON.stringify(expect);
  if (a !== b) {
    console.error('FAIL ' + label);
    console.error('  got     ' + a);
    console.error('  expect  ' + b);
    fail++;
  }
}

// 1. 模板 → 表单值：列表拍平、缺失留空
const values = templateValues(FIELDS, TEMPLATE);
check('模板预填-列表拍平', values.personality, '温柔、热情、活泼');
check('模板预填-文本原样', values.background, '18岁舞蹈系女大学生');
check('模板预填-缺失字段留空', values.values === undefined ? '' : values.values, '真诚、忠诚');

// 2. 模板缺字段时不能残留上一个模板的值
const partial = templateValues(FIELDS, {name: 'X'});
check('缺字段留空', partial.personality, '');
check('缺字段留空-文本', partial.background, '');

// 3. 表单值 → 定义
const def = buildDefinition(FIELDS, {
  name: '  小林  ', identity: '同学', personality: '安静、 细心 ,',
  background: '', values: '真诚',
});
check('定义-去空格', def.name, '小林');
check('定义-列表拆分', def.personality, ['安静', '细心']);
check('定义-空值不写', 'background' in def, false);
check('定义-单元素列表', def.values, ['真诚']);

// 4. 全空的表单 → 空定义
check('全空表单', buildDefinition(FIELDS, {}), {});

// 5. 往返：模板 → 表单值 → 定义 == 原模板
const round = buildDefinition(FIELDS, templateValues(FIELDS, TEMPLATE));
check('往返一致', round, TEMPLATE);

if (fail) { console.error(fail + ' 个用例失败'); process.exit(1); }
console.log('[OK] ' + 9 + ' 个新建角色表单用例全过');
"""


def extract_fn(src: str, name: str) -> str:
    """从 JS 源码里切出一个函数（按花括号配平）。"""
    idx = src.find("function " + name + "(")
    assert idx >= 0, f"app.js 里找不到函数 {name}"
    i = src.index("{", idx)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[idx:j + 1]
    raise AssertionError(f"{name} 的花括号没配平")


def test_persona_form_logic() -> bool:
    """用 node 跑真实的表单组装逻辑。"""
    node = shutil.which("node")
    if not node:
        print("[SKIP] 环境里没有 node，跳过新建角色表单功能测试")
        return False

    from companion_ever.entrypoints.web.server import WEB_DIR
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    snippet = "\n\n".join([
        extract_fn(js, "templateValues"),
        extract_fn(js, "buildDefinition"),
        HARNESS,
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(snippet)
        path = f.name
    try:
        r = subprocess.run(
            [node, path], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if r.returncode != 0:
        raise AssertionError(f"新建角色表单用例失败:\n{r.stdout}\n{r.stderr}")
    print(r.stdout.strip())
    return True


def test_persona_form_wired_in():
    """静态检查：对话框与三个入口都接上了。"""
    from companion_ever.entrypoints.web.server import WEB_DIR
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for fn in ("openCreatePersona", "pickTemplate", "collectPersonaDefinition",
               "savePersona", "deletePersona", "restorePersona"):
        assert f"function {fn}(" in js, f"app.js 里没有 {fn}"
    assert "cell-new-persona" in js and "openCreatePersona" in js, "新建角色格子没接上"
    assert "btn-persona-delete" in js, "资料页没有删除按钮"
    assert "persona-restore" in js, "没有恢复入口"
    for node_id in ("dialog-persona", "tpl-list", "pf-persona-id", "pf-fields",
                    "btn-persona-save", "btn-persona-cancel", "btn-persona-close"):
        assert f'id="{node_id}"' in html, f"index.html 里没有 {node_id}"
    print("[OK] 新建角色对话框、删除按钮、恢复入口都已接上")


if __name__ == "__main__":
    test_persona_form_wired_in()
    test_persona_form_logic()
    print("[OK] 新建角色对话框前端逻辑")
