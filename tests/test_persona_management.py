"""角色的创建、删除（停用）与恢复——HTTP 接口与业务规则。

覆盖：
1. 模板列表：模板卡片 + 自定义表单的字段描述
2. 两条创建路径：用内置模板 / 自己填的 JSON
3. 删除 = 停用：撤销访问、保留数据；恢复后原样回来
4. 边界：重复创建、停用后同名创建、跨用户、PIN
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_persona_service import context  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from companion_ever.entrypoints.web.server import create_app  # noqa: E402
from companion_ever.config import WebConfig  # noqa: E402


def client_for(ctx):
    return TestClient(create_app(ctx, WebConfig(pin="1234")))


def templates_and_fields(path):
    ctx = context(path)
    try:
        with client_for(ctx) as client:
            client.headers["X-Pin"] = "1234"
            data = client.get("/api/persona-templates").json()
            assert set(data) == {"templates", "fields"}, data
            ids = [t["id"] for t in data["templates"]]
            assert ids == ["hot_girlfriend", "philosophical_mentor"], ids
            for card in data["templates"]:
                assert card["name"] and card["identity"], card
                # 卡片要带完整模板，前端选完能直接预填表单
                assert card["fields"]["name"] == card["name"], card
            keys = [f["key"] for f in data["fields"]]
            assert keys[:2] == ["name", "identity"], keys
            required = {f["key"] for f in data["fields"] if f.get("required")}
            assert required == {"name", "identity"}, required
            # 字段描述的 key 必须都在模板结构里
            for card in data["templates"]:
                assert set(keys) <= set(card["fields"]), (keys, sorted(card["fields"]))
    finally:
        ctx.conn.close()


def create_from_template(path):
    ctx = context(path)
    try:
        with client_for(ctx) as client:
            client.headers["X-Pin"] = "1234"
            resp = client.post("/api/persona/alice",
                                json={"persona_id": "小烟", "template_id": "hot_girlfriend"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "created"
            assert "小烟" in ctx.services.persona.list_user_personas("alice")
            # 模板里写死的用户名占位符要换成实际 user_id
            basics = client.get("/api/persona/alice/小烟").json()["persona_basics"]
            assert basics["name"] == "柳如烟", basics
            assert "周艺科" not in json.dumps(basics, ensure_ascii=False), basics
            assert "alice" in basics["background"], basics
    finally:
        ctx.conn.close()


def create_from_definition(path):
    ctx = context(path)
    try:
        with client_for(ctx) as client:
            client.headers["X-Pin"] = "1234"
            definition = json.dumps({"name": "小林", "identity": "同学",
                                     "personality": ["安静", "细心"]}, ensure_ascii=False)
            resp = client.post("/api/persona/alice",
                                json={"persona_id": "小林", "definition": definition})
            assert resp.status_code == 200, resp.text
            basics = client.get("/api/persona/alice/小林").json()["persona_basics"]
            assert basics["name"] == "小林" and basics["identity"] == "同学", basics
            assert basics["personality"] == ["安静", "细心"], basics
            # 两条路径都不给 → 400
            bad = client.post("/api/persona/alice", json={"persona_id": "无名"})
            assert bad.status_code == 400, bad.text
    finally:
        ctx.conn.close()


def duplicate_and_recreate(path):
    ctx = context(path)
    try:
        with client_for(ctx) as client:
            client.headers["X-Pin"] = "1234"
            again = client.post("/api/persona/alice",
                                json={"persona_id": "one", "template_id": "hot_girlfriend"})
            assert again.status_code == 400, again.text
            assert "已启用" in again.json()["detail"], again.text
            # 停用之后再用同一个标识创建 → 拒绝覆盖，要恢复
            assert client.delete("/api/persona/alice/one").status_code == 200
            blocked = client.post("/api/persona/alice",
                                  json={"persona_id": "one", "template_id": "hot_girlfriend"})
            assert blocked.status_code == 400, blocked.text
            assert "恢复" in blocked.json()["detail"], blocked.text
    finally:
        ctx.conn.close()


def delete_keeps_data_and_restore(path):
    ctx = context(path)
    try:
        with client_for(ctx) as client:
            client.headers["X-Pin"] = "1234"
            # 先留一条笔记，确认删除后数据还在
            from companion_ever.domain.enums import MemoryKind
            note = ctx.services.note.create_note("alice", "one", MemoryKind.FACT, "用户喜欢喝茶")
            assert client.delete("/api/persona/alice/one").status_code == 200
            # 有效角色列表里没有了
            assert "one" not in ctx.services.persona.list_user_personas("alice")
            # 已删除清单里有
            archived = client.get("/api/archived-personas/alice").json()
            assert [a["persona_id"] for a in archived] == ["one"], archived
            assert archived[0]["name"], archived
            # 数据没丢（仓库层能读到；业务层按角色有效性拒绝访问）
            assert ctx.repos.note.get(note) is not None
            try:
                ctx.services.note.get_note(note, "alice")
                raise AssertionError("停用后笔记服务不应再放行")
            except PermissionError:
                pass
            # 停用后不再"拥有"该角色 → 与跨角色一致的 404
            denied = client.get("/api/persona/alice/one")
            assert denied.status_code == 404, denied.text
            # 恢复
            assert client.post("/api/persona/alice/one/restore").status_code == 200
            assert "one" in ctx.services.persona.list_user_personas("alice")
            assert client.get("/api/archived-personas/alice").json() == []
            assert client.get("/api/persona/alice/one").status_code == 200
    finally:
        ctx.conn.close()


def cross_user_and_pin(path):
    ctx = context(path)
    try:
        ctx.repos.user.create("bob")
        with client_for(ctx) as client:
            client.headers["X-Pin"] = "1234"
            # bob 不能动 alice 的角色（路径里的 user 是 bob，persona 是 alice 的）
            assert client.delete("/api/persona/bob/one").status_code == 403
            # alice 的角色还在
            assert "one" in ctx.services.persona.list_user_personas("alice")
            # bob 恢复不存在的设定 → 与"没有这个角色"同样的说法，不泄露别人的数据
            missing = client.post("/api/persona/bob/one/restore")
            assert missing.status_code == 400, missing.text
            assert "没有可恢复" in missing.json()["detail"], missing.text
            # bob 自己没有角色
            assert client.get("/api/archived-personas/bob").json() == []
            assert client.get("/api/archived-personas/nobody").status_code == 403
            # PIN 错 → 401
            bad = TestClient(create_app(ctx, WebConfig(pin="1234")))
            assert bad.get("/api/persona-templates").status_code == 401
            assert bad.post("/api/persona/alice",
                            json={"persona_id": "x", "template_id": "hot_girlfriend"}).status_code == 401
            assert bad.delete("/api/persona/alice/one").status_code == 401
    finally:
        ctx.conn.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="persona-management-") as tmp:
        base = Path(tmp)
        templates_and_fields(base / "templates.db")
        create_from_template(base / "template-create.db")
        create_from_definition(base / "definition-create.db")
        duplicate_and_recreate(base / "duplicate.db")
        delete_keeps_data_and_restore(base / "delete-restore.db")
        cross_user_and_pin(base / "cross-user.db")
    print("[OK] 角色管理：模板列表、两条创建路径、删除即停用（数据保留）、恢复、跨用户与 PIN")
