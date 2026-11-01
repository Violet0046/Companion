"""Subscription and explicit test delivery use cases; no model or scheduler."""
from urllib.parse import urlencode

from companion_ever.infrastructure.web_push import (
    PushDeliveryError, generate_identity, valid_subject, validate_endpoint, validate_subscription,
)


class NotificationService:
    def __init__(self, repo, authorizer, sender, subject=''):
        self.repo = repo
        self.authorizer = authorizer
        self.sender = sender
        self.subject = subject

    def config(self):
        if not valid_subject(self.subject):
            return {'configured': False, 'public_key': None}
        _, public = self.repo.identity(generate_identity)
        return {'configured': True, 'public_key': public}

    def _require_user(self, user):
        if not self.authorizer.user_repo.exists(user):
            raise PermissionError('用户不存在')

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

    def send_test(self, user, persona, endpoint):
        self.authorizer.require_persona(user, persona)
        if not self.config()['configured']:
            raise ValueError('服务器尚未开启通知')
        validate_endpoint(endpoint)
        row = self.repo.claim_test(user, endpoint)
        private, _ = self.repo.identity(generate_identity)
        payload = {
            'title': '同行', 'body': '通知已连接，点这里回到会话。',
            'tag': 'companion-notification-test',
            'url': '/?' + urlencode({'user': user, 'persona': persona, 'open': 'chat'}),
        }
        subscription = {'endpoint': row['endpoint'], 'keys': {'p256dh': row['p256dh'], 'auth': row['auth']}}
        try:
            self.sender.send(subscription, payload, private, self.subject)
        except PushDeliveryError as exc:
            if exc.expired:
                self.repo.delete(user, endpoint, row['revision'])
            raise
        return {'status': 'accepted'}
