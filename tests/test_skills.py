"""tools/skills.py 单元测试。"""

from pathlib import Path

from tools import skills


def _setup_skill(root: Path, dir_name: str, frontmatter: str, body: str = "# Body\n") -> Path:
    skill_dir = root / "skills" / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return skill_dir


def test_skills_list_parses_yaml_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_skills_dir", lambda: tmp_path / "skills")
    assert skills.skills_list({}) == "当前没有可用的 skills。"

    _setup_skill(
        tmp_path,
        "demo-dir",
        'name: demo-skill\ncategory: dev\ndescription: >\n  Line one: with colon\n  and line two.',
    )
    out = skills.skills_list({})
    assert "- demo-skill (dev): Line one: with colon and line two." in out


def test_skill_view_by_dir_or_frontmatter_name_and_attachments(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_skills_dir", lambda: tmp_path / "skills")
    skill_dir = _setup_skill(
        tmp_path,
        "demo-dir",
        "name: alias-name\ndescription: test",
        body="# Hello\n",
    )
    ref = skill_dir / "references" / "guide.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("guide content", encoding="utf-8")
    (skill_dir / ".hidden").write_text("ignore", encoding="utf-8")

    by_alias = skills.skill_view({"name": "alias-name"})
    assert "# Hello" in by_alias
    assert "references/guide.md" in by_alias
    assert ".hidden" not in by_alias

    by_dir = skills.skill_view({"name": "demo-dir", "file_path": "references/guide.md"})
    assert by_dir == "guide content"


def test_skill_view_blocks_path_traversal_and_sensitive_files(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_skills_dir", lambda: tmp_path / "skills")
    (tmp_path / ".env").write_text("SECRET=123", encoding="utf-8")
    skill_dir = _setup_skill(tmp_path, "safe-skill", "name: safe-skill\ndescription: test")
    (skill_dir / ".env").write_text("SECRET=456", encoding="utf-8")

    assert "不存在" in skills.skill_view({"name": "../"})
    assert "不存在" in skills.skill_view({"name": "safe-skill/.."})
    assert "不允许" in skills.skill_view({"name": "safe-skill", "file_path": "../../.env"})
    assert "不允许" in skills.skill_view({"name": "safe-skill", "file_path": str(tmp_path / ".env")})
    assert "错误：" in skills.skill_view({"name": "safe-skill", "file_path": ".env"})
    assert "不存在" in skills.skill_view({"name": "safe-skill", "file_path": "missing.md"})
