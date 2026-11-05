"""Permanent removal of a deactivated identity; caller owns the write transaction.

Shared group messages and other members' notes survive. Exact-ID authorization and
revoked leases fence late model results; public creation never reuses an old ID.
"""

def purge_persona(conn,user,persona):
    scope=(user,persona)
    private="SELECT id FROM conversation WHERE user_id=?1 AND persona_id=?2 AND kind='private'"
    messages=f'SELECT id FROM conversation_log WHERE conversation_id IN ({private})'
    jobs="SELECT j.id FROM memory_job j JOIN conversation c ON c.id=j.conversation_id WHERE c.user_id=? AND j.persona_id=?"
    generations=f'SELECT id FROM generation WHERE conversation_id IN ({private}) OR trigger_message_id IN ({messages}) OR context_message_id IN ({messages})'
    # Preserve accepted-message quota without retaining content or role identifiers.
    conn.execute(f"INSERT INTO deleted_message_usage(user_id,accepted_at,count) SELECT user_id,created_at,count(*) FROM conversation_log WHERE conversation_id IN ({private}) AND role='user' GROUP BY user_id,created_at ON CONFLICT(user_id,accepted_at) DO UPDATE SET count=count+excluded.count",scope)
    conn.execute("UPDATE generation SET status='cancelled',lease_expires_at=NULL,opportunity_json=NULL WHERE actor_persona_id=? AND conversation_id IN (SELECT id FROM conversation WHERE user_id=?) AND status IN ('queued','running')",(persona,user))
    conn.execute('DELETE FROM push_delivery WHERE user_id=? AND persona_id=?',scope)
    conn.execute(f'DELETE FROM push_delivery WHERE generation_id IN ({generations})',scope)
    conn.execute('DELETE FROM task_event WHERE attempt_id IN (SELECT id FROM task_attempt WHERE user_id=? AND persona_id=?)',scope)
    conn.execute('DELETE FROM task_attempt WHERE user_id=? AND persona_id=?',scope)
    conn.execute('DELETE FROM summary_checkpoint WHERE user_id=? AND persona_id=?',scope)
    conn.execute('DELETE FROM summary_job WHERE user_id=? AND persona_id=?',scope)
    # Deleting notes fires journal/evidence triggers. Remove the resulting journal afterwards.
    conn.execute('DELETE FROM note WHERE user_id=? AND persona_id=?',scope)
    conn.execute('DELETE FROM memory_block WHERE user_id=? AND persona_id=?',scope)
    conn.execute('DELETE FROM fact_change WHERE user_id=? AND persona_id=?',scope)
    # Other roles cannot own private evidence, but clean legacy references without deleting their notes.
    conn.execute(f'DELETE FROM note_evidence WHERE message_id IN ({messages})',scope)
    for table,column in [('note_link','job_id'),('note_revision','job_id'),('fact_change','source_job_id')]:
        conn.execute(f'UPDATE {table} SET {column}=NULL WHERE {column} IN ({jobs})',scope)
    conn.execute(f'DELETE FROM memory_job WHERE id IN ({jobs})',scope)
    conn.execute('DELETE FROM user_settings WHERE user_id=? AND persona_id=?',scope)
    conn.execute('DELETE FROM group_invitation WHERE persona_id=? AND generation_id IN (SELECT g.id FROM generation g JOIN conversation c ON c.id=g.conversation_id WHERE c.user_id=?)',(persona,user))
    conn.execute(f'DELETE FROM group_invitation WHERE generation_id IN ({generations})',scope)
    conn.execute(f'UPDATE group_invitation SET source_message_id=NULL WHERE source_message_id IN ({messages})',scope)
    conn.execute('DELETE FROM conversation_membership_history WHERE persona_id=? AND conversation_id IN (SELECT id FROM conversation WHERE user_id=?)',(persona,user))
    conn.execute('DELETE FROM conversation_member WHERE persona_id=? AND conversation_id IN (SELECT id FROM conversation WHERE user_id=?)',(persona,user))
    # Break message/generation cycles before deleting the private container.
    conn.execute(f'UPDATE conversation_log SET generation_id=NULL WHERE generation_id IN ({generations})',scope)
    conn.execute(f'DELETE FROM generation WHERE id IN ({generations})',scope)
    conn.execute(f'UPDATE conversation_log SET quoted_message_id=NULL WHERE quoted_message_id IN ({messages})',scope)
    conn.execute(f'UPDATE conversation_log SET reply_to_message_id=NULL WHERE reply_to_message_id IN ({messages})',scope)
    conn.execute(f'DELETE FROM thinking_log WHERE message_id IN ({messages})',scope)
    conn.execute(f'DELETE FROM conversation_log WHERE conversation_id IN ({private})',scope)
    conn.execute(f'DELETE FROM conversation WHERE id IN ({private})',scope)
    conn.execute('DELETE FROM persona_definition WHERE user_id=? AND persona_id=?',scope)
