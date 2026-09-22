"""每届电影节的章程参数。

结论（入选/获奖）必须能回查到当时适用的章程版本，
因此章程是不可变对象：生效后只能以新版本取代，旧版本继续留档。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class EditionRules:
    edition: str
    version: str
    eligible_countries: frozenset[str]
    sections: frozenset[str]
    award_sections: frozenset[str]
    required_materials: frozenset[str]
    # 可评审还要求这些分项本身齐全
    required_detail_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    submission_deadline: date | None = None
    notification_date: date | None = None

    def material_label(self, key: str) -> str:
        return MATERIAL_LABELS.get(key, key)


# 报名分项 → 中文说明
MATERIAL_LABELS: dict[str, str] = {
    "screening_file": "展映影片文件",
    "screener": "初筛样片",
    "dialogue_list": "对白台本",
    "subtitle_file": "字幕文件",
    "director_statement": "导演阐述",
    "credits": "主创与联合制作名单",
    "rights_proof": "权利证明",
    "screening_authorization": "放映授权书",
    "premiere_statement": "首映状态声明",
    "tech_spec": "技术规格单",
    "submission_declaration": "报名声明",
    "still": "剧照",
    "trailer": "预告片",
    "poster": "海报",
}

DEFAULT_REQUIRED = (
    "screener",
    "dialogue_list",
    "rights_proof",
    "screening_authorization",
    "premiere_statement",
    "submission_declaration",
    "credits",
)

DEFAULT_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "credits": ("directors",),
    "rights_proof": ("holders",),
    "screening_authorization": ("scope",),
    "premiere_statement": ("status",),
    "submission_declaration": ("submitter", "accepted_rules_version"),
}


def default_rules(
    edition: str = "第12届",
    version: str = "2026.1",
    *,
    countries: set[str] | None = None,
    sections: set[str] | None = None,
    deadline: date | None = None,
    notification: date | None = None,
) -> EditionRules:
    return EditionRules(
        edition=edition,
        version=version,
        eligible_countries=frozenset(countries or {"中国", "埃及", "法国", "阿根廷", "伊朗"}),
        sections=frozenset(sections or {"金丝路奖竞赛", "短片竞赛", "全景展映", "主宾国展映"}),
        award_sections=frozenset({"金丝路奖竞赛", "短片竞赛"}),
        required_materials=frozenset(DEFAULT_REQUIRED),
        required_detail_keys=dict(DEFAULT_DETAIL_KEYS),
        submission_deadline=deadline,
        notification_date=notification,
    )
