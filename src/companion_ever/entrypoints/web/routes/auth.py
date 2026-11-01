"""Same-origin browser login; all private routes share the same identity guard."""
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from companion_ever.accounts.service import AuthError, COOKIE, MAX_PASSWORD, MAX_SECONDS, MIN_PASSWORD

router = APIRouter(prefix='/api/auth')


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD)
    endpoint: str = Field(default='', max_length=4096)


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD)
    new_password: str = Field(min_length=MIN_PASSWORD, max_length=MAX_PASSWORD)


class LogoutIn(BaseModel):
    endpoint: str = Field(default='', max_length=4096)


def check_origin(connection, *, required=False):
    origin = connection.headers.get('origin')
    cfg = connection.app.state.web_cfg
    expected = cfg.public_origin or (('https' if connection.url.scheme in ('https', 'wss') else 'http') + '://' + connection.headers.get('host', ''))
    if (required and not origin) or (origin and origin != expected):
        raise HTTPException(403, '请求来源不允许')
    if cfg.secure_cookie and origin and urlsplit(origin).scheme != 'https':
        raise HTTPException(403, '登录需要 HTTPS')


async def authorize(request: Request = None, websocket: WebSocket = None):
    connection = request if request is not None else websocket
    path = connection.url.path
    if not (path.startswith('/api/') or path == '/ws/chat'):
        return
    public = path in ('/api/health', '/api/auth/login')
    try:
        if request is not None and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            check_origin(connection)
            if path == '/api/auth/login' and request.headers.get('content-type', '').split(';')[0] != 'application/json':
                raise HTTPException(415, '请使用 JSON 登录')
        if public:
            return
        if websocket is not None:
            check_origin(connection, required=True)
        service = connection.app.state.accounts
        account = await run_in_threadpool(service.authenticate, connection.cookies.get(COOKIE, ''))
        connection.state.account = account
        if account['must_change_password'] and path not in ('/api/auth/me', '/api/auth/password', '/api/auth/logout', '/api/auth/logout-all'):
            raise HTTPException(403, '请先修改初始密码')
        if request is not None and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('x-csrf-token', '')
            if not supplied.isascii() or not secrets.compare_digest(supplied, account['csrf_token']):
                raise HTTPException(403, '页面验证已失效，请刷新重试')
        targets = [connection.path_params.get('user_id')]
        for key in ('user', 'user_id'):
            targets.extend(connection.query_params.getlist(key))
        if request is not None and request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if request.headers.get('content-type', '').split(';')[0] == 'application/json':
                try:
                    payload = await request.json()
                except ValueError:
                    raise HTTPException(400, 'JSON 格式不正确') from None
                if isinstance(payload, dict):
                    targets.extend(payload.get(key) for key in ('user', 'user_id'))
        if any(target and target != account['user_id'] for target in targets):
            raise HTTPException(403, '无权访问其他账号的数据')
    except AuthError as exc:
        if websocket is not None:
            raise WebSocketException(code=4401, reason='请重新登录') from None
        raise HTTPException(exc.status, str(exc)) from None
    except HTTPException as exc:
        if websocket is not None:
            raise WebSocketException(code=4403, reason='访问被拒绝') from None
        raise


@router.post('/login')
def login(request: Request, response: Response, payload: LoginIn):
    accounts = request.app.state.accounts
    try:
        token = accounts.login(payload.username, payload.password, request.client.host if request.client else 'unknown')
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    try:
        previous = accounts.authenticate(request.cookies.get(COOKIE, ''))
    except AuthError:
        previous = None
    if previous:
        accounts.logout(previous, endpoint=payload.endpoint)
        from .chat import revalidate_connections
        revalidate_connections(previous['user_id'])
    response.set_cookie(COOKIE, token, max_age=MAX_SECONDS, httponly=True,
                        secure=request.app.state.web_cfg.secure_cookie, samesite='strict', path='/')
    return {'ok': True}


@router.get('/me')
def me(request: Request):
    account = request.state.account
    return {'user_id': account['user_id'], 'username': account['login_name'],
            'must_change_password': bool(account['must_change_password']), 'csrf_token': account['csrf_token']}


@router.post('/password')
def password(request: Request, response: Response, payload: PasswordIn):
    try:
        request.app.state.accounts.change_password(request.state.account, payload.current_password, payload.new_password)
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    from .chat import revalidate_connections
    revalidate_connections(request.state.account['user_id'])
    response.delete_cookie(COOKIE, path='/', secure=request.app.state.web_cfg.secure_cookie, httponly=True, samesite='strict')
    return {'ok': True}


@router.post('/logout')
@router.post('/logout-all')
def logout(request: Request, response: Response, payload: LogoutIn = LogoutIn()):
    from .chat import revalidate_connections
    all_devices = request.url.path.endswith('logout-all')
    user = request.state.account['user_id']
    request.app.state.accounts.logout(request.state.account, all_devices=all_devices, endpoint=payload.endpoint)
    revalidate_connections(user)
    response.delete_cookie(COOKIE, path='/', secure=request.app.state.web_cfg.secure_cookie, httponly=True, samesite='strict')
    return {'ok': True}
