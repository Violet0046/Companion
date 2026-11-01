"""Enqueue or inspect proactive work; the running application owns execution."""
import json


def cmd_proactive(args):
    from companion_ever.entrypoints.cli import _build_app
    ctx=_build_app(args.db)
    try:
        if args.proactive_cmd=='status':
            g=ctx.services.chat.generations.get(args.task)
            if not g or g['origin']!='proactive':
                raise ValueError('主动任务不存在')
            result={key:g[key] for key in ('id','user_id','persona_id','status','scheduled_at','expires_at','last_error')}
            result['message_ids']=[m['id'] for m in ctx.services.chat.generations.outputs(g['id'])]
            result['notifications']=ctx.services.notifications.repo.deliveries(g['id'])
        elif args.proactive_cmd=='check':
            result=ctx.services.proactive.check(args.user,args.persona,test=args.test)
        else:
            g=ctx.services.proactive.trigger(args.user,args.persona,delay=args.delay,test=args.test,request_id=args.request_id)
            result={key:g[key] for key in ('id','status','scheduled_at','expires_at')}
            result['note']='任务已保存，由运行中的服务执行；测试会调用模型并写入真实聊天记录'
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,PermissionError) as exc:
        print(str(exc))
        return 1
    finally:
        ctx.close()
