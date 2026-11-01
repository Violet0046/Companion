"""PIN-protected device subscription and test notification endpoints."""
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from companion_ever.entrypoints.web.deps import check_pin
from companion_ever.infrastructure.web_push import PushDeliveryError

router = APIRouter(prefix='/api/notifications', tags=['notifications'])


class DeviceIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    endpoint: str = Field(min_length=1, max_length=4096)


class KeysIn(BaseModel):
    p256dh: str = Field(min_length=1, max_length=128)
    auth: str = Field(min_length=1, max_length=64)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4096)
    keys: KeysIn


class SubscribeIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    subscription: SubscriptionIn


class TestIn(DeviceIn):
    persona_id: str = Field(min_length=1, max_length=200)


def service(request, pin):
    check_pin(pin, request.app.state.web_cfg)
    result = request.app.state.ctx.services.notifications
    if result is None:
        raise HTTPException(503, '服务器尚未开启通知')
    return result


def invoke(action, *args):
    try:
        return action(*args)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except PushDeliveryError as exc:
        raise HTTPException(410 if exc.expired else 502, str(exc)) from None


@router.get('/config')
def config(request: Request, x_pin: str = Header(default='')):
    return service(request, x_pin).config()


@router.post('/status')
def status(request: Request, payload: DeviceIn, x_pin: str = Header(default='')):
    return invoke(service(request, x_pin).status, payload.user_id, payload.endpoint)


@router.post('/subscriptions')
def subscribe(request: Request, payload: SubscribeIn, x_pin: str = Header(default='')):
    return invoke(service(request, x_pin).subscribe, payload.user_id, payload.subscription.model_dump())


@router.delete('/subscriptions')
def unsubscribe(request: Request, payload: DeviceIn, x_pin: str = Header(default='')):
    return invoke(service(request, x_pin).unsubscribe, payload.user_id, payload.endpoint)


@router.post('/test')
def test(request: Request, payload: TestIn, x_pin: str = Header(default='')):
    return invoke(service(request, x_pin).send_test, payload.user_id, payload.persona_id, payload.endpoint)
