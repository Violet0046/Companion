"""Enqueue or inspect proactive work; the running application owns execution."""
import json


def cmd_proactive(args):
    from companion_ever.entrypoints.cli import _build_app
    ctx=_build_app(args.db)
    try:
        if args.proactive_cmd in ('check','trigger'):args.persona=ctx.services.persona.resolve_name(args.user,args.persona)
        if args.proactive_cmd=='groups':
            result=[{'id':g['id'],'name':g['name'],'proactive_enabled':bool(g['proactive_enabled']),
                'members':[m['persona_id'] for m in g['members']]} for g in ctx.services.groups.list(args.user)]
        elif args.proactive_cmd=='status':
            g=ctx.services.chat.generations.get(args.task)
            if not g or g['origin']!='proactive':
                raise ValueError('主动任务不存在')
            result={key:g[key] for key in ('id','user_id','persona_id','status','scheduled_at','expires_at','model_requests','last_error')}
            opportunity=json.loads(g['opportunity_json']) if g['opportunity_json'] else {}
            result['source_conversation_id']=opportunity.get('source',{}).get('id')
            result['destination']={'kind':g['conversation_kind'],'conversation_id':g['conversation_id']} if g['status']=='completed' else None
            result['message_ids']=[m['id'] for m in ctx.services.chat.generations.outputs(g['id'])]
            result['notifications']=ctx.services.notifications.repo.deliveries(g['id'])
        elif args.proactive_cmd=='check':
            result=ctx.services.proactive.check(args.user,args.persona,test=args.test,conversation_id=args.conversation_id)
        else:
            options=dict(delay=args.delay,test=args.test,request_id=args.request_id)
            g=ctx.services.proactive.trigger(args.user,args.persona,conversation_id=args.conversation_id,**options)
            result={key:g[key] for key in ('id','status','scheduled_at','expires_at')}
            result['note']='任务已保存，由运行中的服务执行；测试会调用模型并写入真实聊天记录'
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,PermissionError) as exc:
        print(str(exc))
        return 1
    finally:
        ctx.close()
