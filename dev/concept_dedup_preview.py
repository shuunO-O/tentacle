"""概念去重/审计的纯内存合成预览；不导入图谱或模型运行时。"""

from copy import deepcopy
import time

from fastapi import HTTPException, Request


def attach_concept_dedup(app, authenticated):
    now = time.time()
    rule = "concept-retrieval-entry-v2"
    preview = {"left": {"id": "concept_1", "name": "SillyTavern", "aliases": [], "events": ["合成记忆：配置软件的提示词。"]},
               "right": {"id": "concept_2", "name": "sillytavern", "aliases": [], "events": ["合成记忆：调整同一软件的界面。"]},
               "fingerprint": "synthetic", "conflicts": []}
    score = {"probabilities": {"0": 0.04, "1": 0.08, "2": 0.88}, "score": 1.84, "confidence": 0.9}
    pairs = [{"id": "synthetic-pair", "state": "pending", "operation": None, "choice": None,
              "data": {"preview": preview, "score": score, "reason": "uncertain", "rule_version": rule}}]
    entries = [
        {"id": "synthetic-evaluation", "created": now - 20, "actor": "automatic", "source": "history",
         "action": "evaluate", "status": "succeeded", "data": {
             "incoming": preview["left"], "candidates": [preview["right"]], "scores": [score], "rule_version": rule}},
        {"id": "synthetic-manual-merge", "created": now - 50, "actor": "manual", "source": "history",
         "action": "merge", "status": "succeeded", "data": {"previous": {"preview": {
             "left": {"id": "concept_3", "name": "系统提示词"},
             "right": {"id": "concept_4", "name": "项目提示词文件"}}},
             "result": {"keep_id": "concept_3", "drop_id": "concept_4"}, "rule_version": rule}},
        {"id": "synthetic-alignment", "created": now - 90, "actor": "automatic", "source": "alignment",
         "action": "alignment_save", "status": "succeeded", "data": {
             "summary": "合成记忆：保存一次项目配置讨论。", "event_id": "event_1", "rule_version": rule,
             "decisions": [{"mention": "浏览器工具", "target_id": "concept_5", "action": "join", "status": "aligned"}]}},
    ]
    # 足够测试分页，并且保留两个分类及两条业务来源。
    for index in range(30):
        row = deepcopy(entries[index % 3])
        row.update(id=f"synthetic-history-{index}", created=now - 100 - index)
        entries.append(row)

    def auth(request):
        if not authenticated(request):
            raise HTTPException(401, "Synthetic preview authentication required")

    @app.get("/api/memory/dedup")
    async def status(request: Request, state: str = "pending", offset: int = 0):
        auth(request)
        selected = [p for p in pairs if state == "all" or p["state"] == state]
        return {"run": None, "pairs": selected[offset:offset + 30],
                "counts": {s: sum(p["state"] == s for p in pairs) for s in ["pending", "merged", "independent"]}}

    @app.get("/api/memory/dedup/audit")
    async def audit(request: Request, actor: str = "all", offset: int = 0):
        auth(request)
        selected = [row for row in entries if actor == "all" or row["actor"] == actor]
        return {"entries": selected[offset:offset + 30], "total": len(selected), "offset": offset,
                "counts": {a: sum(row["actor"] == a for row in entries) for a in ["manual", "automatic"]}}

    @app.post("/api/memory/dedup/{pair_id}/{action}")
    async def operate(pair_id: str, action: str, request: Request):
        auth(request)
        body = await request.json()
        pair = pairs[0]
        old = deepcopy(pair["data"])
        if action == "reevaluate":
            pair["data"]["score"] = {"probabilities": {"0": 0.01, "1": 0.01, "2": 0.98}, "score": 1.97}
            pair["data"]["reason"] = "same"
        elif action == "decide":
            pair["state"] = "merged" if body["action"] == "merge" else "independent"
            if body["action"] == "merge":
                pair["data"]["result"] = {"keep_id": body["keep_id"], "drop_id": "concept_2"}
        entries.insert(0, {"id": f"synthetic-operation-{len(entries)}", "created": time.time(), "actor": "manual",
                           "source": "history", "action": body.get("action", action), "status": "succeeded",
                           "data": {"previous": old, "result": deepcopy(pair["data"]), "rule_version": rule}})
        return {"state": pair["state"]}
