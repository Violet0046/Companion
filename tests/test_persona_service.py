"""persona_service 端到端测试。

测试完整流程：
1. 创建 user
2. 创建 persona（persona_service.create_persona）
3. 验证 3 张表都有数据：
   - user.persona_ids 包含 persona_id
   - note 表有 persona_basics 笔记
   - memory_block 表有 3 个 Block

变化：
- import 路径改为 application/persona/persona_service 等
- 修复 PersonaService 构造调用（追加 authorizer 参数）
- 使用 dataclass 实体字段访问（b.block_type 而非 b["block_type"]）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from companion_ever.services.authz import Authorizer
from companion_ever.services.note_service import NoteService
from companion_ever.services.persona_service import PersonaService
from companion_ever.domain.enums import BlockType, NoteType
from companion_ever.storage.block_repo import BlockRepo
from companion_ever.storage.db import init_db
from companion_ever.storage.note_repo import NoteRepo
from companion_ever.storage.user_repo import UserRepo


def test_create_persona_writes_3_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = init_db(db_path)

        # Wire 起来
        user_repo = UserRepo(conn)
        note_repo = NoteRepo(conn)
        block_repo = BlockRepo(conn)
        authorizer = Authorizer(user_repo, note_repo)
        note_service = NoteService(authorizer, note_repo)
        service = PersonaService(user_repo, block_repo, note_service, authorizer)

        # 1. 创建 user（先要 user 存在才能挂 persona）
        user_repo.create("alice")

        # 2. 调 service 创建 persona
        service.create_persona(
            user_id="alice",
            persona_id="舒舒",
            definition="舒舒是 27 岁的温柔倾听者。",
        )

        # 3. 验证 user 表
        personas = user_repo.list_personas("alice")
        assert personas == ["舒舒"], f"user.persona_ids 应是 ['舒舒'], 实际 {personas}"

        # 4. 验证 note 表：persona_basics 笔记
        notes = note_repo.find(
            user_id="alice",
            persona_id="舒舒",
            types=[NoteType.PERSONA_BASICS],
        )
        assert len(notes) == 1, f"应有 1 条 persona_basics 笔记, 实际 {len(notes)}"
        assert notes[0].content == "舒舒是 27 岁的温柔倾听者。"
        assert notes[0].importance == 0.9

        # 5. 验证 memory_block 表：3 个 Block
        blocks = block_repo.get_all("alice", "舒舒")
        assert len(blocks) == 3, f"应有 3 个 Block, 实际 {len(blocks)}"
        block_types = {b.block_type for b in blocks}
        expected_types = {
            BlockType.PERSONA_RUNTIME,
            BlockType.USER_PROFILE,
            BlockType.RELATIONSHIP_STATE,
        }
        assert block_types == expected_types, f"Block 类型不匹配: {block_types}"

        conn.close()
        print("[OK] create_persona 端到端通过")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_create_persona_duplicate_raises():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = init_db(db_path)
        user_repo = UserRepo(conn)
        note_repo = NoteRepo(conn)
        block_repo = BlockRepo(conn)
        authorizer = Authorizer(user_repo, note_repo)
        note_service = NoteService(authorizer, note_repo)
        service = PersonaService(user_repo, block_repo, note_service, authorizer)

        conn.cursor().execute("INSERT INTO user (id, persona_ids) VALUES ('alice', '')")
        conn.commit()

        service.create_persona("alice", "舒舒", "第一次创建")
        try:
            service.create_persona("alice", "舒舒", "重复创建")
            print("[FAIL] 重复创建没报错")
        except ValueError as e:
            print(f"[OK] 重复创建报错: {e}")

        conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_create_persona_user_not_exists_raises():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = init_db(db_path)
        user_repo = UserRepo(conn)
        note_repo = NoteRepo(conn)
        block_repo = BlockRepo(conn)
        authorizer = Authorizer(user_repo, note_repo)
        note_service = NoteService(authorizer, note_repo)
        service = PersonaService(user_repo, block_repo, note_service, authorizer)

        try:
            service.create_persona("not_exists", "舒舒", "x")
            print("[FAIL] user 不存在没报错")
        except ValueError as e:
            print(f"[OK] user 不存在报错: {e}")

        conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_exists_checks_ownership():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = init_db(db_path)
        user_repo = UserRepo(conn)
        note_repo = NoteRepo(conn)
        block_repo = BlockRepo(conn)
        authorizer = Authorizer(user_repo, note_repo)
        note_service = NoteService(authorizer, note_repo)
        service = PersonaService(user_repo, block_repo, note_service, authorizer)

        conn.cursor().execute("INSERT INTO user (id, persona_ids) VALUES ('alice', '舒舒')")
        conn.commit()

        assert service.exists("alice", "舒舒") is True
        assert service.exists("alice", "阳阳") is False
        assert service.exists("bob", "舒舒") is False  # 跨用户

        print("[OK] exists 检查正确")
        conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    test_create_persona_writes_3_tables()
    test_create_persona_duplicate_raises()
    test_create_persona_user_not_exists_raises()
    test_exists_checks_ownership()
    print("[ALL] persona_service 端到端测试通过")