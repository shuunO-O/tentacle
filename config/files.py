"""默认配置与本机覆盖的共享读取逻辑；不初始化应用或读取凭据。"""

import hashlib
from copy import deepcopy
from pathlib import Path

import yaml

from config.providers import ConfigurationError
from runtime.paths import get_paths


DEFAULTS_NAME = "config.defaults.yaml"
# 供应商、模型与角色接入配置是一个整体，避免用户删除条目后又被默认配置补回。
MODEL_SECTIONS = {"config_version", "providers", "models", "roles"}


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ConfigurationError("配置管理暂不支持 YAML 合并键，请先在主机展开")
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ConfigurationError("配置必须使用不重复的文本键")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_config(text):
    try:
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise ConfigurationError("配置超过 1 MiB 上限")
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
            raise ConfigurationError("配置管理暂不支持 YAML 别名，请先在主机展开")
        raw = yaml.load(text, Loader=UniqueLoader)
        # 空文件及只有注释的用户文件均表示继承全部默认值；显式 null 不是配置对象。
        if raw is None and yaml.compose(text) is None:
            raw = {}
        if not isinstance(raw, dict) or any(not isinstance(raw.get(k, {}), dict) for k in ("providers", "models", "roles")):
            raise ConfigurationError("配置及 providers/models/roles 必须为对象")
        return raw
    except yaml.YAMLError:
        raise ConfigurationError("YAML 格式无效，请在主机修复后刷新") from None


def merge_config(defaults, user):
    """对象递归覆盖；列表/标量/null 整体替换；用户模型配置整体替换默认接入。"""
    def merge(base, override):
        result = deepcopy(base)
        for key, value in override.items():
            result[key] = (merge(result[key], value)
                           if isinstance(result.get(key), dict) and isinstance(value, dict)
                           else deepcopy(value))
        return result

    replace_models = bool(MODEL_SECTIONS & user.keys())
    base = {k: v for k, v in defaults.items() if not (replace_models and k in MODEL_SECTIONS)}
    return merge(base, user)


class ConfigFiles:
    def __init__(self, path=None):
        paths = get_paths() if path is None else None
        self.path = paths.config_file if paths else Path(path).absolute()
        self.defaults_path = paths.defaults_file if paths else self.path.with_name(DEFAULTS_NAME)
        self.backup_dir = (paths.data_dir if paths else self.path.parent / "data") / "config_backups"
        if self.path == self.defaults_path:
            raise ConfigurationError("默认配置只读，请指定用户配置文件")

    @staticmethod
    def _bytes(path):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConfigurationError("配置必须是普通文件，不能是符号链接")
        return path.read_bytes() if path.exists() else None

    @staticmethod
    def _text(data):
        try:
            return data.decode("utf-8-sig") if data is not None else ""
        except UnicodeDecodeError:
            raise ConfigurationError("配置必须使用 UTF-8 编码") from None

    def read(self):
        defaults_data = self._bytes(self.defaults_path)
        user_data = self._bytes(self.path)
        if defaults_data is None and user_data is None:
            raise ConfigurationError("默认配置与用户配置均不存在")
        defaults = parse_config(self._text(defaults_data)) if defaults_data is not None else {}
        text = self._text(user_data)
        user = parse_config(text)
        # 包含存在状态与两份原始文件，默认配置升级也会使旧预览失效。
        revision = hashlib.sha256()
        for data in (defaults_data, user_data):
            revision.update(b"missing\0" if data is None else b"file\0" + len(data).to_bytes(8, "big") + data)
        return revision.hexdigest(), text, merge_config(defaults, user), defaults, user

    def effective(self, text):
        return merge_config(self.read()[3], parse_config(text))
