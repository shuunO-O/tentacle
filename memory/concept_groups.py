"""等价概念共享检索入口，原节点及关系始终保留。"""

import unicodedata


def name_key(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def group_key(node):
    return node.get("equivalence_group") or node["id"]


def similarity(expression, parameter="vector", dimensions=1024):
    # 与现有 Neo4j cosine 分数保持同一 [0, 1] 尺度。
    return f"((1.0 + array_cosine_similarity({expression}, CAST(${parameter} AS DOUBLE[{dimensions}])))/2.0)"
