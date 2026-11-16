"""
系统提示词构建器

按固定顺序组合多个区块：
  soul / master / memory / guild / available_skills / sisuation
其中 available_skills 从 skills/ 目录下各 skill 的 SKILL.md 动态扫描。

启动及切换 session 时构建，session 内缓存；keep_in_mind 和 situation_notes
工具只修改对应 .md 文件，不立即刷新当前 session 的 system_prompt。
同时把最终提示词写入项目根目录 tmp/system_prompt_preview.md 用于测试预览。
"""

import logging
import os
import re
import tempfile
from pathlib import Path

from runtime.paths import APP_ROOT, state_path
from typing import Any

logger = logging.getLogger(__name__)

# 提示词里代表对话双方的占位符；具体称呼由配置 app.user_name / app.agent_name 提供。
USER_PLACEHOLDER = "{{user_name}}"
AGENT_PLACEHOLDER = "{{agent_name}}"
DEFAULT_USER_NAME = "user"
DEFAULT_AGENT_NAME = "assistant"


def resolve_names(config: dict | None = None) -> tuple[str, str]:
    """返回 (user_name, agent_name)；配置为空时用中性称呼，避免提示词出现空占位。"""
    if config is None:
        from config.manager import ConfigManager

        config = ConfigManager().snapshot()
    app = (config or {}).get("app") or {}
    user_name = str(app.get("user_name") or "").strip() or DEFAULT_USER_NAME
    agent_name = str(app.get("agent_name") or "").strip() or DEFAULT_AGENT_NAME
    return user_name, agent_name


def render_names(text: str, config: dict | None = None) -> str:
    """替换提示词中的称呼占位符。"""
    if USER_PLACEHOLDER not in text and AGENT_PLACEHOLDER not in text:
        return text
    user_name, agent_name = resolve_names(config)
    names = {"user_name": user_name, "agent_name": agent_name}
    return re.sub(r"\{\{(user_name|agent_name)\}\}", lambda match: names[match[1]], text)


# 除 available_skills 外，按此顺序读取 prompt/同名文件.md。
# tool_description 的指导由 guild 承载，不再注入 system prompt。
SECTIONS = ["soul", "master", "memory", "guild"]

# master / memory 文件内的小标题
SECTION_HEADINGS = {
    "master": "## 个人画像",
    "memory": "## 重要记忆",
}

# 占位提示，用于空文件
PLACEHOLDER = "（暂无条目）"


class SystemPromptBuilder:
    """
    动态 system_prompt 构建器。

    提供：
    - build()：按顺序拼接各区块并写入临时预览文件
    - add_entry / remove_entry / list_entries：供 keep_in_mind 工具维护 master/memory
    - read_situation / write_situation：供 situation_notes 工具读取或整体替换近况
    """

    def __init__(
        self,
        prompt_dir: str = "prompt",
        skills_dir: str = "skills",
        preview_dir: str = "tmp",
        preview_filename: str = "system_prompt_preview.md",
        manager: Any | None = None,
    ):
        self.prompt_dir = state_path(prompt_dir)
        self.skills_dir = state_path(skills_dir)
        self.preview_path = state_path(preview_dir) / preview_filename
        self._cache: str | None = None
        self.manager = manager

    def build(self, *, force: bool = False) -> str:
        """构建并缓存完整提示词；切换 session 时用 force 重新读取所有区块。"""
        if self._cache is not None and not force:
            return self._cache

        parts: list[str] = []

        # 各语义区块统一用 XML 闭合标签包裹。
        for name in SECTIONS:
            content = self._load_section(name).strip()
            if content:
                parts.append(f"<{name}>\n{content}\n</{name}>")

        # available_skills 动态生成
        skills = self._load_skills()
        skills_content = self._format_skills(skills)
        parts.append(f"<available_skills>\n{skills_content}\n</available_skills>")

        # 近况放在最后；沿用现有的一次构建与缓存逻辑。
        situation = self.read_situation().strip()
        parts.append(f"<sisuation>\n{situation}\n</sisuation>")

        prompt = render_names("\n\n".join(parts) + "\n",
                              self.manager.snapshot() if self.manager is not None else None)
        self._cache = prompt
        self._write_preview(prompt)
        return prompt

    def read_situation(self) -> str:
        """读取完整近况；尚未创建文件时返回空字符串。"""
        try:
            return self._section_path("situation").read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write_situation(self, content: str) -> None:
        """原子替换完整近况，失败保留旧文件，不刷新已构建的提示词。"""
        if not isinstance(content, str):
            raise TypeError("近况内容必须是字符串")
        path = self._section_path("situation")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".situation-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def list_entries(self, section: str) -> list[str]:
        """列出 master 或 memory 中的条目。"""
        path = self._section_path(section)
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                entries.append(stripped[2:])
        return entries

    def add_entry(self, section: str, entry: str) -> bool:
        """向 master 或 memory 添加一条条目；重复则不添加。"""
        path = self._section_path(section)
        self._ensure_section_file(path, section)

        lines = path.read_text(encoding="utf-8").splitlines()

        # 已存在则不再添加
        for line in lines:
            if line.strip() == f"- {entry}":
                return False

        # 移除占位提示（如果有）
        filtered = [line for line in lines if PLACEHOLDER not in line]
        filtered.append(f"- {entry}")

        path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
        return True

    def remove_entry(self, section: str, entry: str) -> bool:
        """从 master 或 memory 移除一条条目；没有则返回 False。"""
        path = self._section_path(section)
        if not path.exists():
            return False

        lines = path.read_text(encoding="utf-8").splitlines()
        heading = SECTION_HEADINGS.get(section, f"## {section}")

        removed = False
        filtered = []
        for line in lines:
            if line.strip() == f"- {entry}":
                removed = True
                continue
            filtered.append(line)

        if not removed:
            return False

        # 如果删完没有条目，补回占位提示
        if not any(line.strip().startswith("- ") for line in filtered):
            filtered = [heading, "", PLACEHOLDER]

        path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
        return True

    def _section_path(self, section: str) -> Path:
        return self.prompt_dir / f"{section}.md"

    def set_plugin_extensions(
        self,
        *,
        skill_dirs: list[Path] | None = None,
        guild_fragments: list[tuple[str, str]] | None = None,
    ) -> None:
        """设置已启用插件提供的 skills 目录与 guild 提示词片段。"""
        self._plugin_skill_dirs = list(skill_dirs or [])
        self._plugin_guild_fragments = list(guild_fragments or [])
        self._cache = None

    def clear_plugin_extensions(self) -> None:
        """清除已注册的插件 skills 与提示词扩展。"""
        self._plugin_skill_dirs = []
        self._plugin_guild_fragments = []
        self._cache = None

    def _load_section(self, name: str) -> str:
        path = self._section_path(name)
        if name == "guild" and not path.exists():
            path = APP_ROOT / "prompt/guild.example.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
        else:
            logger.warning(f"提示词区块文件不存在: {path}")
            content = ""
        if name == "guild":
            fragments = getattr(self, "_plugin_guild_fragments", ())
            if fragments:
                extra = "\n\n".join(
                    f"<!-- plugin:{pid} -->\n{frag.strip()}"
                    for pid, frag in fragments
                    if frag and frag.strip()
                )
                if extra:
                    content = f"{content.rstrip()}\n\n{extra}" if content.strip() else extra
        return content

    def _ensure_section_file(self, path: Path, section: str):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            heading = SECTION_HEADINGS.get(section, f"## {section}")
            path.write_text(f"{heading}\n\n{PLACEHOLDER}\n", encoding="utf-8")

    def _load_skills(self) -> list[dict[str, Any]]:
        """扫描 skills/ 及已启用插件 skills/ 下各子目录的 SKILL.md，返回 name/description 列表。"""
        skills: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        roots: list[Path] = [self.skills_dir, *getattr(self, "_plugin_skill_dirs", ())]
        for root in roots:
            if not root.exists():
                continue
            for item in sorted(root.iterdir()):
                if not item.is_dir():
                    continue
                skill_file = item / "SKILL.md"
                if not skill_file.exists():
                    continue
                try:
                    text = skill_file.read_text(encoding="utf-8")
                    name, description = self._parse_skill(text)
                    resolved_name = name or item.name
                    if resolved_name in seen_names:
                        continue
                    seen_names.add(resolved_name)
                    skills.append({
                        "name": resolved_name,
                        "description": description or "",
                    })
                except Exception as e:
                    logger.warning(f"读取 skill 文件失败 {skill_file}: {e}")
        return skills

    def _parse_skill(self, text: str) -> tuple[str, str]:
        """解析 SKILL.md：优先读 YAML frontmatter，否则取第一个标题 + 第一段。"""
        lines = text.strip().splitlines()
        name = ""
        description = ""

        # 尝试 YAML frontmatter
        if lines and lines[0].strip() == "---":
            end = next(
                (i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
                None,
            )
            if end:
                for line in lines[1:end]:
                    stripped = line.strip()
                    if stripped.startswith("name:"):
                        name = stripped.split(":", 1)[1].strip()
                    elif stripped.startswith("description:"):
                        description = stripped.split(":", 1)[1].strip()
                if name or description:
                    return name, description

        # 回退：第一个 # 标题作为 name
        for line in lines:
            m = re.match(r"^#\s+(.+)$", line.strip())
            if m:
                name = m.group(1).strip()
                break

        # 第一个非标题段落作为 description
        desc_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                if desc_lines:
                    break
                continue
            desc_lines.append(stripped)
        description = " ".join(desc_lines)

        return name, description

    def _format_skills(self, skills: list[dict[str, Any]]) -> str:
        if not skills:
            return "（暂无可用 skills）"
        return "\n".join(
            f"- {skill.get('name', 'unknown')}: {skill.get('description', '')}"
            for skill in skills
        )

    def _write_preview(self, prompt: str):
        """把构建好的 system_prompt 写入临时预览文件，覆盖不保留历史。"""
        try:
            self.preview_path.parent.mkdir(parents=True, exist_ok=True)
            self.preview_path.write_text(prompt, encoding="utf-8")
        except Exception as e:
            logger.warning(f"写入临时预览文件失败 {self.preview_path}: {e}")
