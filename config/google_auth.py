"""Google 服务账号 JSON 凭据加载与 httpx 令牌刷新传输器。"""

import os

import httpx

from config.providers import CredentialConfigurationError

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
DEFAULT_CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
_credential_envs = {DEFAULT_CREDENTIALS_ENV}


class _HttpxAuthResponse:
    """适配 google-auth 所需的最小同步响应对象。"""

    __slots__ = ("_response",)

    def __init__(self, response: httpx.Response):
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self):
        return self._response.headers

    @property
    def data(self) -> bytes:
        return self._response.content


class HttpxAuthRequest:
    """复用 httpx 的惰性同步传输器，供 google-auth 刷新服务账号令牌。"""

    def __init__(self, proxy: str | None = None, *, timeout: float = 120.0, transport=None):
        self.proxy = proxy
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                proxy=self.proxy or None,
                trust_env=self.proxy is None,
                timeout=self.timeout,
                transport=self._transport,
            )
        return self._client

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        req_timeout = self.timeout if timeout is None else timeout
        try:
            response = self._get_client().request(
                method,
                url,
                content=body,
                headers=headers,
                timeout=req_timeout,
            )
            return _HttpxAuthResponse(response)
        except httpx.HTTPError as exc:
            from google.auth.exceptions import TransportError

            raise TransportError(exc) from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def make_refresh_request(proxy: str | None = None, *, timeout: float = 120.0, transport=None) -> HttpxAuthRequest:
    return HttpxAuthRequest(proxy=proxy, timeout=timeout, transport=transport)


def protect_credentials(raw):
    """将已配置的路径变量纳入统一附件访问保护，不读取凭据文件。"""
    for provider in raw.get("providers", {}).values():
        protect_credential_env(provider.get("credentials_env"))


def protect_credential_env(name):
    if isinstance(name, str) and name:
        _credential_envs.add(name)


def credential_paths():
    return [os.environ[name] for name in _credential_envs if os.environ.get(name)]


def load_credentials(credentials_env="", project=None):
    """从显式环境变量或 GOOGLE_APPLICATION_CREDENTIALS 指向的服务账号 JSON 加载凭据。"""
    env_name = credentials_env or DEFAULT_CREDENTIALS_ENV
    protect_credential_env(env_name)
    path = os.environ.get(env_name, "").strip()
    if not path:
        raise CredentialConfigurationError(f"此 Google 连接的服务账号 JSON 路径环境变量未设置: {env_name}")
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("Google 服务账号认证需要安装 google-auth（pip install -r requirements.txt）") from exc
    try:
        return service_account.Credentials.from_service_account_file(
            path,
            scopes=SCOPES,
            quota_project_id=project,
        )
    except Exception:
        raise CredentialConfigurationError("此 Google 连接的服务账号 JSON 凭据文件无法加载，请检查主机配置") from None
