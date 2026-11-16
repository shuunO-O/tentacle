"""
skills 工具

- skills_list()：扫描 skills/ 目录，返回 name + description 列表
- skill_view(name, file_path=None)：查看 SKILL.md 全文或 skill 目录内指定文件
"""

import logging
from pathlib import Path

import yaml

from attachments.outgoing import check_file_access
from runtime.paths import state_path

logger = logging.getLogger(__name__)

_IGNORED_PARTS = {"__pycache__", ".git", ".venv"}
_plugin_skills_dirs: list[Path] = []


def set_plugin_skills_dirs(dirs: list[Path]) -> None:
    """设置已启用插件提供的额外 skills 根目录列表。"""
    _plugin_skills_dirs[:] = [Path(d) for d in dirs]


def _skills_dir() -> Path:
    """返回项目状态目录下的 skills/ 路径。"""
    return state_path("skills")


def _all_skills_dirs() -> list[Path]:
    return [_skills_dir(), *_plugin_skills_dirs]


def _parse_frontmatter(text: str) -> dict[str, str]:
    """解析 SKILL.md 顶部的 YAML frontmatter，提取 name/description/category。"""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    end = next(
        (i for i, line in enumerate(lines[1:], start=1) if line.strip() in {"---", "..."}),
        None,
    )
    if end is None:
        return {}

    try:
        parsed = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        logger.warning(f"解析 skill frontmatter 失败: {exc}")
        return {}

    if not isinstance(parsed, dict):
        return {}

    result: dict[str, str] = {}
    for key in ("name", "description", "category"):
        value = parsed.get(key)
        if value is not None:
            result[key] = " ".join(str(value).split())
    return result


def _read_text(path: Path) -> str:
    """读取文本文件，失败时返回可读错误信息。"""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"读取文件失败: {path.name}，原因: {exc}"


def _skill_dir(name: str) -> Path | None:
    """按目录名或 frontmatter name 定位 skill 目录，禁止越界路径。"""
    if not isinstance(name, str):
        return None
    cleaned = name.strip()
    if not cleaned or cleaned in {".", ".."} or any(sep in cleaned for sep in ("/", "\\", "\x00")):
        return None

    for skills_root in _all_skills_dirs():
        if not skills_root.is_dir():
            continue
        resolved_root = skills_root.resolve()

        direct = skills_root / cleaned
        try:
            resolved_direct = direct.resolve()
        except OSError:
            resolved_direct = None
        if (
            resolved_direct is not None
            and resolved_direct.is_relative_to(resolved_root)
            and resolved_direct != resolved_root
            and direct.is_dir()
            and (direct / "SKILL.md").is_file()
        ):
            return direct

        for item in sorted(skills_root.iterdir()):
            if not item.is_dir():
                continue
            skill_file = item / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                if not item.resolve().is_relative_to(resolved_root):
                    continue
            except OSError:
                continue
            frontmatter = _parse_frontmatter(_read_text(skill_file))
            if frontmatter.get("name") == cleaned:
                return item
    return None


def _resolve_skill_file(skill_dir: Path, rel_path: str) -> tuple[Path | None, str | None]:
    """校验并解析 skill 目录内的相对文件路径。"""
    if not isinstance(rel_path, str) or not rel_path.strip() or "\x00" in rel_path:
        return None, "错误：file_path 必须是非空相对路径。"
    candidate_rel = Path(rel_path.strip())
    if candidate_rel.is_absolute():
        return None, "错误：不允许使用绝对路径。"

    skill_root = skill_dir.resolve()
    try:
        target = (skill_root / candidate_rel).resolve()
    except OSError as exc:
        return None, f"错误：无效路径：{exc}"

    if not target.is_relative_to(skill_root) or target == skill_root:
        return None, "错误：不允许访问 skill 目录以外的文件。"

    try:
        check_file_access(target)
    except PermissionError as exc:
        return None, f"错误：{exc}"

    if not target.is_file():
        return None, f"错误：文件不存在：{rel_path}"

    return target, None


def _list_attachments(skill_dir: Path) -> list[str]:
    """列出 skill 目录下除 SKILL.md 外的文件清单（相对路径）。"""
    if not skill_dir.exists():
        return []
    skill_root = skill_dir.resolve()
    attachments: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.name == "SKILL.md":
            continue
        rel = path.relative_to(skill_dir)
        if any(part in _IGNORED_PARTS or part.startswith(".") for part in rel.parts):
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(skill_root):
            continue
        try:
            check_file_access(resolved)
        except PermissionError:
            continue
        attachments.append(rel.as_posix())
    return attachments


def skills_list(args: dict) -> str:
    """列出所有可用 skill 的 name 和 description。"""
    items: list[str] = []
    seen_names: set[str] = set()
    for skills_dir in _all_skills_dirs():
        if not skills_dir.is_dir():
            continue
        resolved_root = skills_dir.resolve()
        for item in sorted(skills_dir.iterdir()):
            if not item.is_dir():
                continue
            try:
                if not item.resolve().is_relative_to(resolved_root):
                    continue
            except OSError:
                continue
            skill_file = item / "SKILL.md"
            if not skill_file.is_file():
                continue

            frontmatter = _parse_frontmatter(_read_text(skill_file))
            name = frontmatter.get("name") or item.name
            if name in seen_names:
                continue
            seen_names.add(name)
            description = frontmatter.get("description") or ""
            category = frontmatter.get("category") or "uncategorized"
            items.append(f"- {name} ({category}): {description}")

    if not items:
        return "当前没有可用的 skills。"

    return "## 可用 skills\n\n" + "\n".join(items) + "\n\n提示：使用 skill_view(name) 查看完整内容。"


def skill_view(args: dict) -> str:
    """查看指定 skill 的完整内容或目录内的具体文件。"""
    name = args.get("name")
    file_path = args.get("file_path")

    if not isinstance(name, str) or not name.strip():
        return "错误：必须提供有效的 name 参数。"

    skill_dir = _skill_dir(name)
    if skill_dir is None:
        return f"错误：skill '{name}' 不存在。"

    if file_path is not None:
        if not isinstance(file_path, str):
            return "错误：file_path 必须是字符串。"
        if file_path.strip():
            target, error = _resolve_skill_file(skill_dir, file_path)
            if error:
                return error
            return _read_text(target)

    target, error = _resolve_skill_file(skill_dir, "SKILL.md")
    if error:
        return error
    content = _read_text(target)

    attachments = _list_attachments(skill_dir)
    if attachments:
        content += "\n\n## 附带文件\n\n" + "\n".join(f"- {a}" for a in attachments)
        content += "\n\n提示：使用 skill_view(name, file_path=...) 查看具体文件。"

    return content


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="skills_list",
        description="列出可用 skills，返回 name、category 和一句话 description。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        func=skills_list,
        is_async=False,
    )
    registry.register(
        name="skill_view",
        description="查看指定 skill 的 SKILL.md 全文；传入 file_path 可查看目录内参考文件。",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "skill 名称，对应 skills/ 目录下的子目录名或 SKILL.md frontmatter 中的 name 字段。",
                },
                "file_path": {
                    "type": "string",
                    "description": "skill 目录内的相对文件路径，如 references/example.md。不传则返回 SKILL.md 全文。",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        func=skill_view,
        is_async=False,
    )
