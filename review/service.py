"""征集审定后台的应用服务。

所有写操作都翻译为审计事件追加，读状态来自事件投影；
进程内无其他可变事实来源，可整体序列化并在别处重放。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from .audit import EventLog, utc_now
from .errors import (
    AuthorizationError,
    ConflictError,
    DuplicateSubmissionError,
    LifecycleError,
    SecrecyError,
    ValidationError,
)
from .models import SubmissionView
from .projector import Projector
from .rules import MATERIAL_LABELS, EditionRules, default_rules


def _rules_from_payload(d: dict[str, Any]) -> EditionRules:
    """重放时从章程事件还原不可变章程对象。"""
    return EditionRules(
        edition=d["edition"],
        version=d["version"],
        eligible_countries=frozenset(d["eligible_countries"]),
        sections=frozenset(d["sections"]),
        award_sections=frozenset(d["award_sections"]),
        required_materials=frozenset(d["required_materials"]),
        required_detail_keys={k: tuple(v) for k, v in d.get("required_detail_keys", {}).items()},
        submission_deadline=date.fromisoformat(d["submission_deadline"]) if d.get("submission_deadline") else None,
        notification_date=date.fromisoformat(d["notification_date"]) if d.get("notification_date") else None,
    )

ORGANIZER_ROLES = frozenset({"organizer", "chair", "admin"})
MEDIA_KINDS = frozenset({"screener", "screening_file"})
# 授权用途 → 可接触的文件类型
AUTH_SCOPE_FOR_KIND = {"screener": "review", "screening_file": "screening"}


class FestivalService:
    def __init__(
        self,
        rules: EditionRules | None = None,
        log: EventLog | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.clock = clock or utc_now
        self.log = log or EventLog(clock=self.clock)
        self.rules_registry: dict[tuple[str, str], EditionRules] = {}
        self.projector = Projector()
        # 从历史事件重建章程注册表与读模型；空库则把初始章程作为第一条事件留档。
        for event in self.log.events():
            if event.action == "rules_published":
                self._register_rules(_rules_from_payload(event.data))
        if len(self.log) == 0:
            initial = rules or default_rules()
            self._register_rules(initial)
            self._append("rules_published", self._rules_payload(initial), actor="registry")
        else:
            self.projector.apply_log(self.log)

    # ------------------------------------------------------------------ 章程

    def _register_rules(self, rules: EditionRules) -> None:
        self.rules_registry[(rules.edition, rules.version)] = rules

    def publish_rules(self, rules: EditionRules) -> None:
        """公布新版章程；旧版本继续留档供结论回查。"""
        self._register_rules(rules)
        self._append("rules_published", self._rules_payload(rules), actor="registry")

    def _rules_payload(self, rules: EditionRules) -> dict[str, Any]:
        return {
            "edition": rules.edition,
            "version": rules.version,
            "eligible_countries": sorted(rules.eligible_countries),
            "sections": sorted(rules.sections),
            "award_sections": sorted(rules.award_sections),
            "required_materials": sorted(rules.required_materials),
            "required_detail_keys": {k: list(v) for k, v in rules.required_detail_keys.items()},
            "submission_deadline": rules.submission_deadline.isoformat() if rules.submission_deadline else None,
            "notification_date": rules.notification_date.isoformat() if rules.notification_date else None,
        }

    def rules_for(self, sid: str) -> EditionRules:
        view = self._view(sid)
        return self.rules_registry[self._edition_of(view), view.rules_version]

    def _edition_of(self, view: SubmissionView) -> str:
        for edition, versions in self.projector.rules_versions.items():
            if view.rules_version in versions:
                return edition
        # 未在事件中公布过的初始章程
        for (edition, version) in self.rules_registry:
            if version == view.rules_version:
                return edition
        raise LifecycleError(f"报名 {view.submission_id} 适用章程 {view.rules_version} 已不可考")

    # ------------------------------------------------------------- 报名与分项

    def register_submission(
        self,
        submission_id: str,
        *,
        title: str,
        submitter: str,
        directors: list[str],
        countries: list[str],
        section: str,
        materials: dict[str, dict[str, Any]] | None = None,
        origin_key: str | None = None,
        actor: str | None = None,
    ) -> None:
        """登记报名；主创/权利/首映/授权/声明等通过 materials 分项分别保存。"""
        if not title or not directors or not countries:
            raise ValidationError("片名、主创导演与国家地区为必填项")
        rules = next(reversed(self.rules_registry.values()))
        if section not in rules.sections:
            raise ValidationError(f"单元 {section} 不在章程 {rules.version} 允许范围内")
        unknown = set(countries) - rules.eligible_countries
        if unknown:
            raise ValidationError(f"国家地区不在参评范围：{sorted(unknown)}")
        if submission_id in self.projector.submissions:
            raise DuplicateSubmissionError(f"报名号已存在：{submission_id}")

        # 同一作品允许多个报名主体分别报名（用于识别同源版本）；
        # 同一报名主体就同一作品重复报名则直接拒绝。
        for existing in self.projector.submissions.values():
            same_work = (
                existing.title.strip().lower() == title.strip().lower()
                and tuple(existing.directors) == tuple(directors)
            )
            if same_work and submitter in existing.submitters:
                raise DuplicateSubmissionError(
                    f"报名主体 {submitter} 已就同一作品报名（{existing.submission_id}）"
                )

        self._append(
            "submission_registered",
            {
                "submission_id": submission_id,
                "title": title,
                "rules_version": rules.version,
                "directors": list(directors),
                "countries": list(countries),
                "submitters": [submitter],
                "section": section,
                "origin_key": origin_key,
            },
            actor=actor or submitter,
        )
        for key, item in (materials or {}).items():
            self.provide_material(
                submission_id, key,
                detail=item.get("detail", {}),
                present=item.get("present", True),
                actor=actor or submitter,
            )

    def provide_material(
        self,
        sid: str,
        key: str,
        *,
        detail: dict[str, Any] | None = None,
        present: bool = True,
        actor: str = "submitter",
    ) -> None:
        """补交或更新某一分项（权利、首映声明、授权书、字幕、技术规格、报名声明……）。"""
        self._require_active(sid)
        self._append(
            "material_provided",
            {"submission_id": sid, "key": key, "present": present, "detail": detail or {}},
            actor=actor,
        )

    def request_supplement(self, sid: str, keys: list[str], *, reason: str = "", actor: str = "selection") -> None:
        """选片人列明缺件、发出补件要求（追加记录）。"""
        if not keys:
            raise ValidationError("补件要求至少包含一个分项")
        self._append(
            "material_supplement_requested",
            {"submission_id": sid, "keys": list(keys), "reason": reason},
            actor=actor,
        )

    # ------------------------------------------------------------------ 媒体

    def upload_media(
        self,
        sid: str,
        kind: str,
        *,
        file_id: str,
        filename: str,
        sha256: str,
        size_bytes: int = 0,
        note: str = "",
        subtitle_languages: list[str] | None = None,
        origin_key: str | None = None,
        submitter: str | None = None,
        actor: str | None = None,
    ) -> None:
        """上传影片文件；按文件摘要登记，重复摘要直接识别为同一文件。"""
        if kind not in MEDIA_KINDS:
            raise ValidationError(f"未知文件类型：{kind}")
        self._require_active(sid)
        if not _is_sha256(sha256):
            raise ValidationError("文件摘要必须为 64 位十六进制 SHA-256")
        view = self._view(sid)
        duplicate = self._find_file_by_hash(sha256)
        if duplicate is not None:
            other_sid, version = duplicate
            # 完全相同的文件再次上传：不产生新版本，挂到同源聚类即可发现。
            if other_sid == sid and version.filename == filename:
                raise DuplicateSubmissionError(
                    f"文件 {filename} 摘要已存在于报名 {sid}（{version.note or version.file_id}）"
                )
        data = {
            "submission_id": sid,
            "kind": kind,
            "file_id": file_id,
            "filename": filename,
            "sha256": sha256.lower(),
            "size_bytes": size_bytes,
            "note": note,
            "subtitle_languages": list(subtitle_languages or ()),
            "origin_key": origin_key,
            "submitter": submitter or (view.submitters[-1] if view.submitters else actor or "submitter"),
        }
        self._append("media_version_uploaded", data, actor=actor or data["submitter"])

    def replace_media(
        self, sid: str, kind: str, *, old_file_id: str, file_id: str, filename: str, sha256: str,
        note: str = "", subtitle_languages: list[str] | None = None, reason: str = "",
        actor: str = "submitter",
    ) -> None:
        """换版：旧版停用但整条留档，新版成为有效版本（追加记录）。"""
        self._require_active(sid)
        view = self._view(sid)
        if not any(v.file_id == old_file_id and v.active for v in view.versions(kind)):
            raise LifecycleError(f"待替换的有效版本不存在：{old_file_id}")
        if not _is_sha256(sha256):
            raise ValidationError("文件摘要必须为 64 位十六进制 SHA-256")
        self._append(
            "media_version_replaced",
            {
                "submission_id": sid, "kind": kind,
                "old_file_id": old_file_id, "file_id": file_id, "filename": filename,
                "sha256": sha256.lower(), "note": note,
                "subtitle_languages": list(subtitle_languages or ()),
                "submitter": view.submitters[-1] if view.submitters else actor,
                "reason": reason,
            },
            actor=actor,
        )

    # ------------------------------------------------------------- 撤回与单元

    def withdraw(self, sid: str, *, reason: str = "", actor: str = "submitter") -> None:
        view = self._require_active(sid)
        if view.decision is not None:
            raise LifecycleError("已审定影片的撤回须通过复核流程处理")
        self._append("withdrawn", {"submission_id": sid, "reason": reason}, actor=actor)

    def change_section(self, sid: str, to_section: str, *, reason: str = "", actor: str = "selection") -> None:
        """调整单元：以追加记录体现，原单元与原因全部留痕。"""
        self._require_active(sid)
        rules = self.rules_for(sid)
        if to_section not in rules.sections:
            raise ValidationError(f"目标单元 {to_section} 不在章程允许范围内")
        view = self._view(sid)
        if to_section == view.section:
            raise ValidationError("新旧单元相同")
        self._append(
            "section_changed",
            {"submission_id": sid, "from": view.section, "to": to_section, "reason": reason},
            actor=actor,
        )

    # ------------------------------------------------------------------ 回避

    def declare_conflict(self, judge_id: str, sid: str, *, reason: str, actor: str | None = None) -> None:
        """评委/组委会登记回避关系；必须在分配影片之前完成才生效。"""
        self._view(sid)
        self._append(
            "conflict_declared",
            {"juror_id": judge_id, "submission_id": sid, "reason": reason},
            actor=actor or judge_id,
        )

    def assign_judges(self, sid: str, judge_ids: list[str], *, actor: str = "jury-office") -> None:
        """分配评委。回避关系在此之前生效：存在任一未解除回避即整体拒绝并留痕。"""
        if len(set(judge_ids)) != len(judge_ids):
            raise ValidationError("同一评委被重复分配")
        view = self._view(sid)
        if view.withdrawn:
            raise LifecycleError("影片已撤回，不能分配评审")
        if view.scores and set(judge_ids) != set(view.assigned_judges):
            raise LifecycleError("已有评委评分，调整评审阵容须通过复核流程")
        blocking: list[dict[str, str]] = []
        for judge_id in judge_ids:
            entry = self.projector.conflicts.get(judge_id, {}).get(sid)
            if entry is not None:
                blocking.append({"juror_id": judge_id, "reason": entry["reason"]})
        if blocking:
            self._append(
                "assignment_blocked",
                {"submission_id": sid, "judge_ids": list(judge_ids), "blocking": blocking},
                actor=actor,
            )
            raise ConflictError(f"分配被回避关系拦截：{blocking}")
        missing = self.missing_materials(sid)
        if missing:
            raise LifecycleError(f"影片尚不可评审，缺件 {len(missing)} 项，不能进入分配")
        self._append(
            "judges_assigned", {"submission_id": sid, "judge_ids": list(judge_ids)}, actor=actor
        )

    # ------------------------------------------------------------- 授权访问

    def access_media(self, judge_id: str, sid: str, kind: str = "screener") -> dict[str, Any]:
        """评委取影片文件；只能接触已分配、已获对应授权且影片有效的内容。"""
        view = self._view(sid)

        def deny(reason: str) -> AuthorizationError:
            self._append(
                "access_denied",
                {"juror_id": judge_id, "submission_id": sid, "kind": kind, "reason": reason},
                actor=judge_id,
            )
            return AuthorizationError(reason)

        if judge_id not in view.assigned_judges:
            raise deny("评委未被分配到该影片")
        if view.withdrawn:
            raise deny("影片已撤回")
        if self.missing_materials(sid):
            raise deny("影片仍有缺件，不属于可评审内容")
        auth = view.materials.get("screening_authorization")
        if auth is None or not auth.present:
            raise deny("缺少放映授权书")
        scope = AUTH_SCOPE_FOR_KIND[kind]
        if scope not in auth.detail.get("scope", ()):
            raise deny(f"放映授权范围不含 {scope} 用途")
        version = view.active_version(kind)
        if version is None:
            raise deny(f"没有有效的{ MATERIAL_LABELS.get(kind, kind)}")
        return {
            "file_id": version.file_id,
            "filename": version.filename,
            "sha256": version.sha256,
            "subtitle_languages": list(version.subtitle_languages),
            "note": version.note,
        }

    # ------------------------------------------------------------------ 评分

    def submit_score(
        self, judge_id: str, sid: str, value: float, *, comment: str = "", actor: str | None = None
    ) -> None:
        self._guard_judge(judge_id, sid)
        view = self._view(sid)
        if view.decision is not None:
            raise LifecycleError("已审定影片的评分变更须先发起复核")
        if not 0 <= float(value) <= 10:
            raise ValidationError("评分须在 0–10 之间")
        self._append(
            "score_submitted",
            {"submission_id": sid, "juror_id": judge_id, "value": float(value), "comment": comment},
            actor=actor or judge_id,
        )

    def correct_score(
        self, judge_id: str, sid: str, new_value: float, *, reason: str, actor: str | None = None
    ) -> None:
        """更正评分：不改原分，追加一条评分并指向被更正的评分事件。"""
        self._guard_judge(judge_id, sid)
        view = self._view(sid)
        if view.decision is not None:
            raise LifecycleError("已审定影片的评分更正须先发起复核")
        previous = view.current_score(judge_id)
        if previous is None:
            raise LifecycleError("该评委尚无评分可更正，应直接提交评分")
        if not 0 <= float(new_value) <= 10:
            raise ValidationError("评分须在 0–10 之间")
        self._append(
            "score_submitted",
            {
                "submission_id": sid, "juror_id": judge_id, "value": float(new_value),
                "comment": f"【更正】{reason}", "supersedes_seq": previous.seq,
            },
            actor=actor or judge_id,
        )

    def _guard_judge(self, judge_id: str, sid: str) -> None:
        view = self._view(sid)
        if view.withdrawn:
            raise LifecycleError("影片已撤回")
        if judge_id not in view.assigned_judges:
            raise AuthorizationError("评委未被分配到该影片")

    # ------------------------------------------------------------------ 复核

    def request_review(self, sid: str, *, reason: str, actor: str) -> None:
        view = self._view(sid)
        if view.open_review():
            raise LifecycleError("已有尚未结论的复核")
        self._append(
            "review_requested", {"submission_id": sid, "reason": reason}, actor=actor
        )

    def resolve_review(self, sid: str, *, conclusion: str, reason: str = "", actor: str = "chair") -> None:
        view = self._view(sid)
        if not view.open_review():
            raise LifecycleError("没有待结论的复核")
        self._append(
            "review_resolved",
            {"submission_id": sid, "conclusion": conclusion, "reason": reason},
            actor=actor,
        )

    # ------------------------------------------------------------- 审定与隔离

    def record_decision(
        self,
        sid: str,
        outcome: str,
        *,
        awards: list[str] | None = None,
        rationale: str = "",
        actor: str = "jury-president",
    ) -> dict[str, Any]:
        """记录审定结论。结论在正式通知前保持隔离，仅组委会可见。"""
        if outcome not in {"入选", "未入选"}:
            raise ValidationError("结论只能是 入选 / 未入选")
        view = self._view(sid)
        if view.withdrawn:
            raise LifecycleError("影片已撤回，不能形成审定结论")
        if self.missing_materials(sid):
            raise LifecycleError("缺件未补齐，不能审定")
        if not view.assigned_judges:
            raise LifecycleError("尚未完成评委分配，不能审定")
        scored = {s.juror_id for s in view.scores}
        if not set(view.assigned_judges) <= scored:
            raise LifecycleError("存在评委尚未独立评分，不能审定")
        if view.open_review():
            raise LifecycleError("复核尚未结论，不能审定")
        awards = list(awards or [])
        if awards and view.section not in self.rules_for(sid).award_sections:
            raise LifecycleError(f"单元 {view.section} 不设奖项")
        if view.decision is not None:
            # 改判只允许由复核结论「推翻原结论」驱动，历史结论照样保留。
            last_resolution = next(
                (r for r in reversed(view.reviews) if r.kind == "resolution"), None
            )
            if last_resolution is None or last_resolution.conclusion != "推翻原结论":
                raise LifecycleError("结论已存在；改判须先经复核得出「推翻原结论」")
            if view.decision.notified:
                raise LifecycleError("结论已正式通知，不能再改")
        event = self._append(
            "decision_recorded",
            {
                "submission_id": sid, "outcome": outcome, "awards": awards,
                "rationale": rationale, "decided_by": actor,
            },
            actor=actor,
        )
        return {"seq": event.seq, "submission_id": sid, "outcome": outcome, "awards": awards}

    def notify_results(self, sids: list[str], *, actor: str = "festival-office") -> None:
        """正式通知：通知发出前未入选结果对外隔离，通知后方可公布。"""
        targets = []
        for sid in sids:
            view = self._view(sid)
            if view.decision is None:
                raise LifecycleError(f"{sid} 尚无审定结论")
            if view.withdrawn:
                raise LifecycleError(f"{sid} 已撤回")
            targets.append(sid)
        self._append("results_notified", {"submission_ids": targets}, actor=actor)

    def announce(self, edition: str, outcomes: list[dict[str, str]], *, actor: str = "festival-office") -> None:
        """公布名单与奖项；每个结论都要能回查章程、有效文件、独立评分与复核过程。"""
        if not outcomes:
            raise ValidationError("公布名单为空")
        verified: list[dict[str, Any]] = []
        for item in outcomes:
            sid = item["submission_id"]
            view = self._view(sid)
            if view.decision is None:
                raise LifecycleError(f"{sid} 没有审定结论，不能公布")
            if not view.decision.notified:
                raise SecrecyError(f"{sid} 尚未正式通知，结论仍在隔离期，禁止公布")
            package = self.evidence_package(sid)
            if not package["valid_files"]:
                raise LifecycleError(f"{sid} 缺少有效影片文件，证据链不完整")
            if len(package["independent_scores"]) < 1:
                raise LifecycleError(f"{sid} 缺少独立评分，证据链不完整")
            if view.open_review():
                raise LifecycleError(f"{sid} 复核未结案，不能公布")
            verified.append({
                "submission_id": sid,
                "title": view.title,
                "outcome": view.decision.outcome,
                "awards": list(view.decision.awards),
                "decision_seq": view.decision.seq,
                "rules_version": view.rules_version,
            })
        self._append(
            "results_announced",
            {"edition": edition, "outcomes": verified},
            actor=actor,
        )

    def get_decision(self, sid: str, *, role: str = "organizer") -> dict[str, Any]:
        """读取结论；隔离期内只有组委会角色可见。"""
        view = self._view(sid)
        if view.decision is None:
            raise LifecycleError(f"{sid} 尚无审定结论")
        if not view.decision.notified and role not in ORGANIZER_ROLES:
            raise SecrecyError("结论尚未正式通知，处于隔离期")
        return {
            "submission_id": sid,
            "outcome": view.decision.outcome,
            "awards": list(view.decision.awards),
            "notified": view.decision.notified,
            "announced_seq": view.decision.announced_seq,
            "seq": view.decision.seq,
        }

    # ------------------------------------------------------------- 状态与报告

    def missing_materials(self, sid: str) -> list[dict[str, str]]:
        """缺件清单：分项 + 中文说明 + 缺失原因。"""
        view = self._view(sid)
        rules = self.rules_for(sid)
        missing: list[dict[str, str]] = []
        for key in sorted(rules.required_materials):
            label = MATERIAL_LABELS.get(key, key)
            if key in MEDIA_KINDS:
                if view.active_version(key) is None:
                    missing.append({"key": key, "label": label, "reason": "未上传有效文件"})
                continue
            record = view.materials.get(key)
            if record is None or not record.present:
                missing.append({"key": key, "label": label, "reason": "尚未提交"})
                continue
            for field_name in rules.required_detail_keys.get(key, ()):
                if not _filled(record.detail.get(field_name)):
                    missing.append(
                        {"key": key, "label": label, "reason": f"缺少必填内容：{field_name}"}
                    )
        return missing

    def readiness_report(self, sid: str) -> dict[str, Any]:
        """当前可审文件与缺件一目了然。"""
        view = self._view(sid)
        rules = self.rules_for(sid)
        valid_files = []
        kinds = (MEDIA_KINDS & rules.required_materials) | set(view.media)
        for kind in sorted(kinds):
            version = view.active_version(kind)
            if version is not None:
                valid_files.append({
                    "kind": kind,
                    "label": MATERIAL_LABELS.get(kind, kind),
                    "file_id": version.file_id,
                    "filename": version.filename,
                    "sha256": version.sha256,
                    "note": version.note,
                    "subtitle_languages": list(version.subtitle_languages),
                    "uploaded_at": version.uploaded_at,
                })
        missing = self.missing_materials(sid)
        return {
            "submission_id": sid,
            "title": view.title,
            "status": self.derived_status(sid),
            "review_ready": not missing and not view.withdrawn,
            "valid_files": valid_files,
            "missing": missing,
            "open_supplement_keys": sorted(view.open_supplement_keys),
        }

    def derived_status(self, sid: str) -> str:
        """由事件事实推导流程状态（与领域资料中的状态集对齐）。"""
        view = self._view(sid)
        if view.withdrawal is not None:
            return "已撤回"
        if view.decision is not None:
            return "已审定"
        missing = self.missing_materials(sid)
        if missing:
            return "待补件" if view.supplement_requests else "已报名"
        if view.assigned_judges:
            return "评审中"
        return "可评审"

    # ------------------------------------------------------------- 同源聚类

    def same_origin_groups(self) -> list[dict[str, Any]]:
        """按文件摘要与作品源标识识别同源版本（导演剪辑版、多字幕、多报名主体）。"""
        nodes: dict[str, tuple[str, str]] = {}  # node key → (sid, file_id)
        for sid, view in self.projector.submissions.items():
            for version in view.versions():
                nodes[f"{sid}|{version.file_id}"] = (sid, version.file_id)
        parent = {key: key for key in nodes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        buckets: dict[tuple[str, Any], list[str]] = {}
        for key, (sid, file_id) in nodes.items():
            view = self.projector.submissions[sid]
            version = next(v for v in view.versions() if v.file_id == file_id)
            for bucket in (
                ("hash", version.sha256),
                ("origin", version.origin_key),
                ("work", self._work_key(view.title, view.directors, view.countries)),
            ):
                if bucket[1]:
                    buckets.setdefault(bucket, []).append(key)
        for members in buckets.values():
            for other in members[1:]:
                union(members[0], other)

        groups: dict[str, list[str]] = {}
        for key in nodes:
            groups.setdefault(find(key), []).append(key)

        result = []
        for members in groups.values():
            if len(members) < 1:
                continue
            entries = []
            submitters: set[str] = set()
            hashes: set[str] = set()
            for key in members:
                sid, file_id = nodes[key]
                view = self.projector.submissions[sid]
                version = next(v for v in view.versions() if v.file_id == file_id)
                submitters.add(version.submitter)
                hashes.add(version.sha256)
                entries.append({
                    "submission_id": sid,
                    "title": view.title,
                    "file_id": file_id,
                    "filename": version.filename,
                    "sha256": version.sha256,
                    "note": version.note,
                    "subtitle_languages": list(version.subtitle_languages),
                    "submitter": version.submitter,
                    "active": version.active,
                    "upload_seq": version.upload_seq,
                })
            if len(entries) == 1 and len(submitters) == 1:
                continue  # 孤立文件不构成同源组
            reasons = []
            if len(hashes) < len(entries):
                reasons.append("文件摘要相同")
            reasons.append("作品源标识/片名主创匹配")
            result.append({
                "reason": reasons,
                "multiple_submitters": sorted(submitters),
                "versions": sorted(entries, key=lambda e: (e["submission_id"], e["upload_seq"])),
            })
        return sorted(result, key=lambda g: g["versions"][0]["submission_id"])

    def _find_file_by_hash(self, digest: str) -> tuple[str, Any] | None:
        for sid, view in self.projector.submissions.items():
            for version in view.versions():
                if version.sha256 == digest.lower():
                    return sid, version
        return None

    # ------------------------------------------------------------------ 回查

    def evidence_package(self, sid: str) -> dict[str, Any]:
        """从结论回查：适用章程、有效影片文件、独立评分、复核过程与全部审计轨迹。"""
        view = self._view(sid)
        rules = self.rules_for(sid)
        valid_files = [
            {
                "kind": kind,
                "file_id": version.file_id,
                "filename": version.filename,
                "sha256": version.sha256,
                "note": version.note,
                "subtitle_languages": list(version.subtitle_languages),
                "upload_event_seq": version.upload_seq,
            }
            for kind in sorted(view.media)
            if (version := view.active_version(kind)) is not None
        ]
        independent_scores = []
        for judge_id in view.assigned_judges:
            current = view.current_score(judge_id)
            if current is not None:
                independent_scores.append({
                    "juror_id": judge_id,
                    "value": current.value,
                    "comment": current.comment,
                    "score_event_seq": current.seq,
                    "corrections": [
                        {"seq": s.seq, "ts": s.ts, "value": s.value, "supersedes_seq": s.supersedes_seq}
                        for s in view.scores
                        if s.juror_id == judge_id and s.supersedes_seq is not None
                    ],
                })
        return {
            "submission_id": sid,
            "title": view.title,
            "applicable_rules": {
                "edition": self._edition_of(view),
                "version": rules.version,
                "required_materials": sorted(rules.required_materials),
                "sections": sorted(rules.sections),
            },
            "materials": {
                key: {
                    "present": record.present,
                    "detail": record.detail,
                    "updated_at": record.updated_at,
                    "update_count": len(record.history) + 1,
                }
                for key, record in sorted(view.materials.items())
            },
            "valid_files": valid_files,
            "all_versions": [
                {
                    "kind": kind, "file_id": v.file_id, "sha256": v.sha256, "note": v.note,
                    "active": v.active, "upload_seq": v.upload_seq,
                }
                for kind in sorted(view.media) for v in view.media[kind]
            ],
            "independent_scores": independent_scores,
            "review_process": [
                {
                    "seq": r.seq, "ts": r.ts, "actor": r.actor, "kind": r.kind,
                    "reason": r.reason, "conclusion": r.conclusion,
                }
                for r in view.reviews
            ],
            "decision": (
                {
                    "outcome": view.decision.outcome,
                    "awards": list(view.decision.awards),
                    "rationale": view.decision.rationale,
                    "seq": view.decision.seq,
                    "ts": view.decision.ts,
                    "notified": view.decision.notified,
                    "announced_seq": view.decision.announced_seq,
                }
                if view.decision is not None else None
            ),
            "audit_trail": [
                {
                    "seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action,
                    "data": e.data, "hash": e.hash,
                }
                for e in self.log.events(sid)
            ],
        }

    # ------------------------------------------------------------- 持久化

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps({"events": self.log.to_list()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(
        cls, path: str | Path, rules: EditionRules | None = None,
        clock: Callable[[], str] | None = None,
    ) -> "FestivalService":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        log = EventLog.from_list(raw["events"], clock=clock)
        svc = cls(rules=rules, log=log, clock=clock)
        return svc

    # ------------------------------------------------------------- 内部工具

    def _append(self, action: str, data: dict[str, Any], *, actor: str):
        event = self.log.append(action, data, actor=actor)
        self.projector.apply(event.seq, event.ts, event.actor, event.action, event.data)
        return event

    def _view(self, sid: str) -> SubmissionView:
        try:
            return self.projector.submissions[sid]
        except KeyError:
            raise LifecycleError(f"未知报名号：{sid}") from None

    def _require_active(self, sid: str) -> SubmissionView:
        view = self._view(sid)
        if view.withdrawn:
            raise LifecycleError("影片已撤回，不能再变更材料")
        return view

    @staticmethod
    def _work_key(title: str, directors: list[str], countries: list[str]) -> tuple[str, ...]:
        return (
            title.strip().lower(),
            tuple(sorted(directors)),
            tuple(sorted(countries)),
        )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True
