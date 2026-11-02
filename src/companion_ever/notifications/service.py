"""Device subscriptions and durable message notification delivery."""
from urllib.parse import urlencode
import threading
import json
import logging

from companion_ever.infrastructure.web_push import (
    PushDeliveryError, generate_identity, valid_subject, validate_endpoint, validate_subscription,
)


class NotificationService:
    def __init__(self, repo, authorizer, sender, subject='', *, delivery_allowed=None):
        self.repo = repo
        self.authorizer = authorizer
        self.sender = sender
        self.subject = subject
        self.delivery_allowed = delivery_allowed
        self.group_delivery_allowed=None
        self._lock=threading.Lock();self._worker=None;self._stop=threading.Event()

    def start(self):
        self._stop.clear()

    def shutdown(self):
        self._stop.set()

    def enqueue(self, user, persona, gid, message_id, body):
        # Called inside the conversation transaction: queue only, never send here.
        self.repo.enqueue(user,persona,gid,message_id,{
            'title':persona, 'body':body[:140], 'tag':'companion-'+gid,
            'url':'/?'+urlencode({'user':user,'persona':persona,'open':'chat'}),
        })

    def enqueue_group(self,user,cid,name,gid,message_id,persona,body):
        self.repo.enqueue(user,persona,gid,message_id,{
            'title':name,'body':body[:140],'tag':'companion-group-'+gid,
            'url':'/?'+urlencode({'user':user,'conversation_id':cid,'open':'chat'}),
        },conversation_id=cid,update_queued=True)

    def wake_group(self,cid):
        self.repo.wake_group(cid)

    def tick(self):
        if self._stop.is_set() or not valid_subject(self.subject) or not self.repo.has_delivery():
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker=threading.Thread(target=self._drain,daemon=True,name='notification-delivery')
            self._worker.start()

    def _drain(self):
        try:
            for _ in range(20):
                if self._stop.is_set():
                    break
                job=self.repo.claim_delivery()
                if job is None:
                    break
                subscription=self.repo.get(job['user_id'],job['endpoint'])
                if subscription is None:
                    self.repo.finish_delivery(job,'skipped','设备订阅已关闭');continue
                if job.get('conversation_kind')=='group':
                    allowed=self.group_delivery_allowed(job['user_id'],job['conversation_id'],job['message_id']) if self.group_delivery_allowed is not None else False
                    if isinstance(allowed,dict):
                        self.repo.defer_delivery(job,allowed['defer_until']);continue
                else:
                    allowed=self.authorizer.owns_persona(job['user_id'],job['persona_id'])
                    if self.delivery_allowed is not None:
                        allowed=allowed and self.delivery_allowed(job['user_id'],job['persona_id'],job['message_id'])
                if not allowed or subscription is None:
                    self.repo.finish_delivery(job,'skipped','已读、已有新输入、角色停用或订阅关闭')
                    continue
                private,_=self.repo.identity(generate_identity)
                try:
                    self.sender.send({'endpoint':subscription['endpoint'],'keys':{'p256dh':subscription['p256dh'],'auth':subscription['auth']}},
                        json.loads(job['payload_json']),private,self.subject)
                    self.repo.finish_delivery(job,'accepted')
                except PushDeliveryError as exc:
                    if exc.expired:
                        self.repo.delete(job['user_id'],job['endpoint'],subscription['revision'])
                    self.repo.finish_delivery(job,'failed' if exc.expired or job['attempt']>=3 else 'queued',str(exc))
        except Exception:
            logging.getLogger(__name__).exception('通知投递执行失败')
        finally:
            self.repo.conn.close_current()

    def config(self):
        if not valid_subject(self.subject):
            return {'configured': False, 'public_key': None}
        _, public = self.repo.identity(generate_identity)
        return {'configured': True, 'public_key': public}

    def _require_user(self, user):
        row = self.repo.conn.execute('SELECT disabled FROM user WHERE id=?', (user,)).fetchone()
        if not row or row['disabled']:
            raise PermissionError('用户不存在或已停用')

    def status(self, user, endpoint):
        self._require_user(user)
        validate_endpoint(endpoint)
        return {'subscribed': self.repo.get(user, endpoint) is not None}

    def subscribe(self, user, subscription):
        self._require_user(user)
        if not self.config()['configured']:
            raise ValueError('服务器尚未开启通知')
        self.repo.save(user, validate_subscription(subscription))
        return {'subscribed': True}

    def unsubscribe(self, user, endpoint):
        self._require_user(user)
        validate_endpoint(endpoint)
        self.repo.delete(user, endpoint)
        return {'subscribed': False}
