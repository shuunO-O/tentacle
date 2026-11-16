"""
消息分段模块（通用，Web 展示层 / QQ 出站共用）

把 agent 输出按空行（markdown 段落语义）拆成多段。
agent 侧上下文与归档不做任何改动，拆分只发生在展示/出站层：
- Web：一条 message 拆多个气泡（web/adapter.py）
- QQ：一次 send_message 拆多条消息发送（plugins/bundled/qqbot/adapter.py `_deliver`）

规则：
- 空行（≥2 个连续换行）即段边界；连续空行不产生空段；段首尾空白 trim
- fenced code block（``` / ~~~ 围栏）内的空行不作为边界，保护代码/JSON 长文
- 拆出段数超过 max_segments 视为整篇长文，不拆整条返回
  （仅批量路径生效；流式路径段已实时发出无法收回，靠围栏保护兜底）
"""

import re

# 围栏行：行首至多 3 空格缩进 + 3 个以上 ` 或 ~（CommonMark 宽松版）
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _match_fence_close(marker: str, rest: str, open_marker: str) -> bool:
    """闭合围栏判定：同类字符、长度不小于开围栏、行尾只允许空白"""
    return (
        marker[0] == open_marker[0]
        and len(marker) >= len(open_marker)
        and not rest.strip()
    )


def split_segments(text: str, max_segments: int = 8) -> list[str]:
    """按空行拆分文本；超上限或不可拆时整条原样返回（trim 后）。"""
    text = (text or "").strip()
    if not text:
        return []

    segments: list[str] = []
    current: list[str] = []
    fence_open = False
    open_marker = ""

    def _flush():
        seg = "\n".join(current).strip()
        current.clear()
        if seg:
            segments.append(seg)

    for line in text.split("\n"):
        if line.strip():
            m = _FENCE_RE.match(line)
            if m:
                marker, rest = m.group(1), m.group(2)
                if not fence_open:
                    fence_open, open_marker = True, marker
                elif _match_fence_close(marker, rest, open_marker):
                    fence_open, open_marker = False, ""
            current.append(line)
        elif not fence_open:
            _flush()
        else:
            current.append(line)  # 围栏内空行：普通内容
    _flush()

    if len(segments) > max_segments:
        # 超过段数上限：判定为整篇长文（文章/日志），不拆
        return [text]
    return segments


def find_confirmed_boundaries(text: str) -> list[int]:
    """
    流式分段用：扫描已累计的文本，返回所有"已确认"的段边界位置。

    边界位置 b 的含义：text[:b] 是一个完整段（含段尾空行），text[b:] 是后续内容。
    "已确认" = 空行序列之后已出现非空行内容；缓冲末尾尚未跟内容的空行序列
    不算（连续空行可能继续延长，段边界发了就无法收回，等下个增量再确认）。
    围栏内空行不算边界。已确认边界位置永不漂移，可安全做增量对比。

    每次对累计缓冲全量重扫：幂等无状态，规避跨增量维护行/围栏状态的漂移 bug；
    常见消息几 KB，逐 token 全扫无感知。
    """
    boundaries: list[int] = []
    fence_open = False
    open_marker = ""
    pos = 0
    pending_gap_end = -1  # 待定空行序列的末尾位置；-1 = 当前无待定空行

    for line in text.split("\n"):
        pos += len(line) + 1  # +1 为换行符（末行无换行，超出仅影响末尾待定值）
        if line.strip():
            # 非空行（含围栏行）：若有待定空行序列，锁定边界
            if pending_gap_end != -1:
                boundaries.append(pending_gap_end)
                pending_gap_end = -1
            m = _FENCE_RE.match(line)
            if m:
                marker, rest = m.group(1), m.group(2)
                if not fence_open:
                    fence_open, open_marker = True, marker
                elif _match_fence_close(marker, rest, open_marker):
                    fence_open, open_marker = False, ""
        elif fence_open:
            pass  # 围栏内空行：普通内容，不构成边界
        else:
            pending_gap_end = pos  # 围栏外空行：待定边界（等后续非空行确认）
    # 末尾待定空行序列不确认（由后续增量或 final 收尾兜底）
    return boundaries
