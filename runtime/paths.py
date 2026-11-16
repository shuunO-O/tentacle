"""Application code and persistent state have independent roots."""

import os
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AppPaths:
    state_root: Path
    app_root: Path = APP_ROOT

    @property
    def config_file(self) -> Path:
        return self.state_root / "config/config.yaml"

    @property
    def defaults_file(self) -> Path:
        return self.app_root / "config.defaults.yaml"

    @property
    def dotenv_file(self) -> Path:
        return self.state_root / "config/.env"

    @property
    def data_dir(self) -> Path:
        return self.state_root / "data"

    @property
    def workspace(self) -> Path:
        return self.state_root / "workspace"


def get_paths() -> AppPaths:
    root = Path(os.environ.get("TENTACLE_HOME", ""))
    if not root.is_absolute():
        raise ValueError("TENTACLE_HOME 必须指向绝对路径的数据根目录")
    root = root.resolve()
    if root == Path(root.anchor) or root.is_relative_to(APP_ROOT) or APP_ROOT.is_relative_to(root):
        raise ValueError("TENTACLE_HOME 必须与程序目录分离")
    return AppPaths(root)


def state_path(path: str | Path) -> Path:
    path = Path(path).expanduser()
    return path if path.is_absolute() else get_paths().state_root / path


def prompt_template(name: str) -> Path:
    relative = Path("prompt") / name
    override = state_path(relative)
    return override if override.is_file() else APP_ROOT / relative
