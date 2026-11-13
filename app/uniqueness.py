"""唯一性口径：同一样方、同一物种仅允许一条汇总记录。

匹配规则：样方、物种分别去除首尾空白后精确匹配（株数不参与匹配，
株数不同也判定为重复）。本模块为纯函数，页面只读检查与事务内
提交前检查共用同一套口径，保证结果一致。
"""

from __future__ import annotations


def _key(record: dict) -> tuple[str, str]:
    return str(record["quadrat"]).strip(), str(record["species"]).strip()


def find_duplicate_groups(records: dict) -> list[dict]:
    """在整批/全库记录中找出重复组。

    records: {id: {"quadrat", "species", "count"}}
    返回（按样方、物种、ID 排序，保证输出稳定）：
        [{"quadrat": "Q1", "species": "羊草",
          "ids": ["R001", "R020"],
          "records": {"R001": {...}, "R020": {...}}}, ...]
    无重复时返回空列表。
    """
    buckets: dict[tuple[str, str], dict] = {}
    for rid, rec in records.items():
        buckets.setdefault(_key(rec), {})[rid] = rec

    groups = []
    for (quadrat, species), members in buckets.items():
        if len(members) >= 2:
            groups.append(
                {
                    "quadrat": quadrat,
                    "species": species,
                    "ids": sorted(members),
                    "records": {rid: members[rid] for rid in sorted(members)},
                }
            )
    groups.sort(key=lambda g: (g["quadrat"], g["species"]))
    return groups


def format_duplicate_message(groups: list[dict], prefix: str = "唯一性检查未通过") -> str:
    """把重复组格式化成给数据员看的提示，含每组全部记录 ID。"""
    parts = []
    for g in groups:
        ids = "、".join(g["ids"])
        parts.append(
            f"样方「{g['quadrat']}」+ 物种「{g['species']}」存在 {len(g['ids'])} 条重复记录"
            f"（记录 ID：{ids}；株数不同也算重复）"
        )
    return f"{prefix}，整批已拒绝、记录未改变：" + "；".join(parts)


class DuplicateError(Exception):
    """写事务提交前的整批最终状态检查发现重复。

    携带重复组供页面展示；事务已由数据访问层回滚，记录保持原样。
    """

    def __init__(self, groups: list[dict]):
        self.groups = groups
        super().__init__(format_duplicate_message(groups))
