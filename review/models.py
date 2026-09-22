"""征集审定领域的读模型（由审计事件重放得到）。"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MediaVersion:
    """一个影片文件版本；换版不覆盖旧版，全部留档。"""

    file_id: str
    kind: str  # screener 初筛样片 / screening_file 展映文件
    filename: str
    sha256: str
    size_bytes: int
    upload_seq: int
    uploaded_at: str
    submitter: str
    note: str = ""  # 如「导演剪辑版」
    subtitle_languages: tuple[str, ...] = ()
    origin_key: str | None = None  # 报名方填报的作品源标识，辅助同源聚类
    active: bool = True


@dataclass(frozen=True)
class MaterialRecord:
    """分项材料的最新状态与提交轨迹。"""

    key: str
    present: bool
    detail: dict[str, Any]
    seq: int
    updated_at: str
    actor: str
    history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ScoreRecord:
    juror_id: str
    value: float
    comment: str
    seq: int
    ts: str
    supersedes_seq: int | None = None  # 更正评分时指向原评分事件序号


@dataclass(frozen=True)
class ReviewRecord:
    """复核过程中的一次动作（发起 / 复核结论）。"""

    seq: int
    ts: str
    actor: str
    kind: str  # request 发起复核 / resolution 复核结论
    reason: str
    conclusion: str | None = None


@dataclass
class Decision:
    outcome: str  # 入选 / 未入选
    awards: tuple[str, ...]
    rationale: str
    seq: int
    ts: str
    decided_by: str
    notified: bool = False
    announced_seq: int | None = None


@dataclass
class SubmissionView:
    submission_id: str
    title: str = ""
    rules_version: str = ""
    directors: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    submitters: list[str] = field(default_factory=list)
    section: str | None = None
    section_history: list[dict[str, Any]] = field(default_factory=list)
    status: str = "已报名"
    withdrawn: bool = False
    withdrawal: dict[str, Any] | None = None
    materials: dict[str, MaterialRecord] = field(default_factory=dict)
    media: dict[str, list[MediaVersion]] = field(default_factory=dict)
    active_seq: dict[str, int] = field(default_factory=dict)
    supplement_requests: list[dict[str, Any]] = field(default_factory=list)
    open_supplement_keys: set[str] = field(default_factory=set)
    assigned_judges: list[str] = field(default_factory=list)
    assignment_seq: int | None = None
    scores: list[ScoreRecord] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)
    decision: Decision | None = None

    # ---- 媒体版本便捷查询 ----
    def versions(self, kind: str | None = None) -> list[MediaVersion]:
        if kind is None:
            return [v for versions in self.media.values() for v in versions]
        return list(self.media.get(kind, ()))

    def active_version(self, kind: str) -> MediaVersion | None:
        seq = self.active_seq.get(kind)
        if seq is None:
            return None
        return next((v for v in self.media.get(kind, ()) if v.upload_seq == seq), None)

    def current_score(self, juror_id: str) -> ScoreRecord | None:
        current = [s for s in self.scores if s.juror_id == juror_id]
        return current[-1] if current else None

    def open_review(self) -> bool:
        """是否存在尚未结论的复核（请求/结论按事件顺序配对）。"""
        requested = False
        for record in self.reviews:
            if record.kind == "request":
                requested = True
            elif record.kind == "resolution":
                requested = False
        return requested

    def snapshot(self) -> dict[str, Any]:
        """供序列化/展示的纯数据快照。"""
        return dataclasses.asdict(self)
