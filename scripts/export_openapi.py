"""离线导出 Tentacle WebUI 的 OpenAPI 规范（供前端生成 TypeScript 类型）。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_openapi_schema() -> dict:
    from web.adapter import ConnectionManager
    from web.schemas import (
        WsDedupProgressData,
        WsEventEnvelope,
        WsMaintenanceUpdatedData,
        WsSubagentUpdatedData,
    )
    from web.server import create_app
    from web.settings import WebSettings

    with tempfile.TemporaryDirectory(prefix="tentacle-openapi-") as tmp:
        tmp_path = Path(tmp)
        settings = WebSettings(str(tmp_path / "webui_settings.json"))
        stub = SimpleNamespace(
            snapshot=lambda *a, **kw: {},
            presentation=lambda *a, **kw: {},
            coding_available=False,
        )
        maintenance = SimpleNamespace(
            snapshot=lambda *a, **kw: {},
            reserve=lambda *a, **kw: {},
            start=lambda *a, **kw: None,
        )
        app = create_app(
            ConnectionManager(),
            SimpleNamespace(get_terminal_history=lambda since=0: [], get_usage_stats=lambda: {}, chat_state=None),
            SimpleNamespace(),
            None,
            None,
            SimpleNamespace(db_path=tmp_path / "chronicle.db", get_active_session=lambda: None, get_recent_rounds=lambda *a: []),
            settings,
            SimpleNamespace(),
            configuration_service=stub,
            subagent_manager=stub,
            maintenance_controller=maintenance,
        )
        schema = app.openapi()

    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in (WsEventEnvelope, WsSubagentUpdatedData, WsDedupProgressData, WsMaintenanceUpdatedData):
        model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        defs = model_schema.pop("$defs", {})
        components[model.__name__] = model_schema
        components.update(defs)
    return schema


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Tentacle Web OpenAPI JSON without booting the runtime.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "frontend" / "src" / "api" / "openapi.json",
        help="Destination path for openapi.json",
    )
    args = parser.parse_args(argv)
    schema = build_openapi_schema()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
