"""Historical summaries stay readable, but only grounded notes enter model context."""
import tempfile
from pathlib import Path
from test_durable_flow import context
from companion_ever.domain.enums import BlockType
from fake_memory import no_memory


def main():
    with tempfile.TemporaryDirectory() as tmp:
        ctx=context(Path(tmp)/'blocks.db')
        try:
            block=ctx.services.block
            ctx.repos.block.upsert('alice','one',BlockType.USER_PROFILE,{'preferences':['旧画像不应灌入模型']})
            prompt=ctx.services.prompt.build('alice','one',query='你好')['system_prompt']
            assert '旧画像不应灌入模型' not in prompt
            assert {b.block_type for b in block.get_blocks('alice','one')} == {BlockType.USER_PROFILE,BlockType.RELATIONSHIP_STATE}
            old=[f'旧偏好{i}' for i in range(8)]
            ctx.repos.block.upsert('alice','one',BlockType.USER_PROFILE,{'preferences':old})
            moments=[f'时刻{i}' for i in range(12)]
            ctx.repos.block.upsert('alice','one',BlockType.RELATIONSHIP_STATE,{'key_moments':moments,'stage_progress':.8})
            summaries={b.block_type:b.fields for b in block.get_blocks('alice','one')}
            ctx.llm.chat.side_effect=no_memory
            ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content='今天有点累')
            out=ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})
            assert out['job_status']=='succeeded' and out['blocks_updated']==0,out
            assert summaries=={b.block_type:b.fields for b in block.get_blocks('alice','one')}
            assert '旧偏好' not in ctx.llm.chat.call_args.args[0][-1]['content']
        finally:
            ctx.conn.close()
    print('[OK] historical block compatibility and grounded prompt isolation')


if __name__=='__main__':
    main()
