"""Google 身份加载；显式连接凭据不修改进程 ADC。"""

import os

import google.auth

from config.providers import CredentialConfigurationError

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_credential_envs = {"GOOGLE_APPLICATION_CREDENTIALS"}


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
    if credentials_env:
        protect_credential_env(credentials_env)
        path = os.environ.get(credentials_env, "")
        if not path:
            raise CredentialConfigurationError("此 Google 连接的凭据路径环境变量未设置")
        try:
            credentials, _ = google.auth.load_credentials_from_file(
                path, scopes=SCOPES, quota_project_id=project)
        except Exception:
            raise CredentialConfigurationError("此 Google 连接的凭据文件无法加载，请检查主机配置") from None
    else:
        credentials, _ = google.auth.default(scopes=SCOPES, quota_project_id=project)
    return credentials
