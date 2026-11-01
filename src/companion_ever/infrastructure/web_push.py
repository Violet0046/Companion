"""Web Push protocol adapter; no database, chat or application dependencies."""
import base64
import json
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from pywebpush import WebPushException, webpush
from requests import RequestException, Session


class PushDeliveryError(RuntimeError):
    def __init__(self, message, *, expired=False):
        super().__init__(message)
        self.expired = expired


def _encode(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def generate_identity():
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(Encoding.DER, PrivateFormat.TraditionalOpenSSL, NoEncryption())
    public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _encode(private), _encode(public)


def valid_subject(value):
    if not value or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme == 'mailto':
        return '@' in parsed.path and not parsed.query and not parsed.fragment
    return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password


def validate_endpoint(endpoint):
    """Subscription input cannot turn the server into an arbitrary HTTP client."""
    if not isinstance(endpoint, str) or len(endpoint) > 4096 or any(c.isspace() for c in endpoint):
        raise ValueError('通知订阅地址无效')
    try:
        url = urlsplit(endpoint)
        allowed = url.hostname in ('web.push.apple.com', 'fcm.googleapis.com', 'updates.push.services.mozilla.com')
        valid = (allowed and url.scheme == 'https' and url.port in (None, 443)
                 and not url.username and not url.password and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('不支持此通知服务地址')
    return endpoint


def validate_subscription(subscription):
    endpoint = validate_endpoint(subscription['endpoint'])
    try:
        def decode(value):
            return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        p256dh = subscription['keys']['p256dh']
        auth = subscription['keys']['auth']
        public = decode(p256dh)
        if len(public) != 65 or len(decode(auth)) != 16:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('通知订阅密钥无效') from exc
    return {'endpoint': endpoint, 'keys': {'p256dh': p256dh, 'auth': auth}}


class _PushSession(Session):
    def request(self, method, url, **kwargs):
        kwargs['allow_redirects'] = False
        return super().request(method, url, **kwargs)


class WebPushSender:
    def send(self, subscription, payload, private_key, subject):
        validate_subscription(subscription)
        try:
            with _PushSession() as session:
                response = webpush(
                    subscription_info=subscription,
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=private_key,
                    vapid_claims={'sub': subject},
                    content_encoding='aes128gcm', timeout=8, ttl=300,
                    requests_session=session,
                )
            if not 200 <= response.status_code < 300:
                raise PushDeliveryError('通知服务未接受请求，请稍后重试')
        except WebPushException as exc:
            response = exc.response
            status = response.status_code if response is not None else None
            if status in (404, 410):
                raise PushDeliveryError('通知订阅已失效，请重新开启通知', expired=True) from None
            if status in (400, 401, 403):
                raise PushDeliveryError('通知服务拒绝请求，请检查服务器推送配置') from None
            raise PushDeliveryError('通知服务暂时不可用，请稍后重试') from None
        except RequestException:
            # Raw exceptions may contain the private subscription endpoint.
            raise PushDeliveryError('暂时无法连接通知服务，请稍后重试') from None
