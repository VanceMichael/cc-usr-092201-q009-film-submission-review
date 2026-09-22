"""把审计事件重放为当前读模型。"""

from __future__ import annotations

from typing import Any

from .audit import EventLog
from .models import (
    Decision,
    MaterialRecord,
    MediaVersion,
    ReviewRecord,
    ScoreRecord,
    SubmissionView,
)


class Projector:
    def __init__(self) -> None:
        self.submissions: dict[str, SubmissionView] = {}
        self.rules_versions: dict[str, set[str]] = {}  # edition → versions
        self.announcements: list[dict[str, Any]] = []
        self.conflicts: dict[str, dict[str, dict[str, Any]]] = {}  # juror → submission → 原因
        self.conflict_log: list[dict[str, Any]] = []  # 回避拦截记录
        self.access_denials: list[dict[str, Any]] = []  # 越权访问拦截记录

    def apply_log(self, log: EventLog) -> "Projector":
        for event in log.events():
            self.apply(event.seq, event.ts, event.actor, event.action, event.data)
        return self

    def apply(self, seq: int, ts: str, actor: str, action: str, data: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{action}", None)
        if handler is None:
            raise ValueError(f"未知事件类型：{action}")
        handler(seq, ts, actor, data)

    # ---- 各事件的折叠规则 ----

    def _on_rules_published(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        self.rules_versions.setdefault(d["edition"], set()).add(d["version"])

    def _on_submission_registered(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        sid = d["submission_id"]
        if sid in self.submissions:
            raise ValueError(f"报名号重复：{sid}")
        self.submissions[sid] = SubmissionView(
            submission_id=sid,
            title=d["title"],
            rules_version=d["rules_version"],
            directors=list(d.get("directors", ())),
            countries=list(d.get("countries", ())),
            submitters=list(d.get("submitters", ())),
            section=d.get("section"),
        )

    def _on_material_supplement_requested(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.supplement_requests.append(
            {"seq": seq, "ts": ts, "actor": actor, "keys": list(d["keys"]), "reason": d.get("reason", "")}
        )
        view.open_supplement_keys.update(d["keys"])

    def _on_material_provided(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        key = d["key"]
        previous = view.materials.get(key)
        history = tuple(previous.history) if previous else ()
        if previous is not None:
            history = history + (
                {"seq": previous.seq, "ts": previous.updated_at, "actor": previous.actor,
                 "present": previous.present, "detail": previous.detail},
            )
        view.materials[key] = MaterialRecord(
            key=key,
            present=bool(d.get("present", True)),
            detail=dict(d.get("detail", {})),
            seq=seq,
            updated_at=ts,
            actor=actor,
            history=history,
        )
        view.open_supplement_keys.discard(key)

    def _add_version(self, view: SubmissionView, d: dict[str, Any], seq: int, ts: str) -> MediaVersion:
        kind = d["kind"]
        upload_seq = len(view.media.get(kind, ())) + 1
        version = MediaVersion(
            file_id=d["file_id"],
            kind=kind,
            filename=d["filename"],
            sha256=d["sha256"],
            size_bytes=d.get("size_bytes", 0),
            upload_seq=upload_seq,
            uploaded_at=ts,
            submitter=d.get("submitter", ""),
            note=d.get("note", ""),
            subtitle_languages=tuple(d.get("subtitle_languages", ())),
            origin_key=d.get("origin_key"),
        )
        view.media.setdefault(kind, []).append(version)
        view.active_seq[kind] = upload_seq
        return version

    def _on_media_version_uploaded(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        self._add_version(self._get(d["submission_id"]), d, seq, ts)

    def _on_media_version_replaced(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        for version in view.media.get(d["kind"], ()):
            if version.file_id == d["old_file_id"]:
                # 旧版整条保留，仅停用。
                version.active = False
        self._add_version(view, d, seq, ts)

    def _on_withdrawn(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.withdrawn = True
        view.withdrawal = {"seq": seq, "ts": ts, "actor": actor, "reason": d.get("reason", "")}

    def _on_section_changed(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.section_history.append(
            {"seq": seq, "ts": ts, "actor": actor, "from": d["from"], "to": d["to"],
             "reason": d.get("reason", "")}
        )
        view.section = d["to"]

    def _on_conflict_declared(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        jury_conflicts = self.conflicts.setdefault(d["juror_id"], {})
        jury_conflicts[d["submission_id"]] = {
            "reason": d.get("reason", ""), "seq": seq, "ts": ts, "actor": actor,
        }

    def _on_assignment_blocked(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        self.conflict_log.append({"seq": seq, "ts": ts, **d})

    def _on_judges_assigned(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.assigned_judges = list(d["judge_ids"])
        view.assignment_seq = seq

    def _on_score_submitted(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.scores.append(
            ScoreRecord(
                juror_id=d["juror_id"],
                value=float(d["value"]),
                comment=d.get("comment", ""),
                seq=seq,
                ts=ts,
                supersedes_seq=d.get("supersedes_seq"),
            )
        )

    def _on_review_requested(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.reviews.append(
            ReviewRecord(seq=seq, ts=ts, actor=actor, kind="request", reason=d.get("reason", ""))
        )

    def _on_review_resolved(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.reviews.append(
            ReviewRecord(
                seq=seq, ts=ts, actor=actor, kind="resolution",
                reason=d.get("reason", ""), conclusion=d.get("conclusion"),
            )
        )

    def _on_decision_recorded(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        view = self._get(d["submission_id"])
        view.decision = Decision(
            outcome=d["outcome"],
            awards=tuple(d.get("awards", ())),
            rationale=d.get("rationale", ""),
            seq=seq,
            ts=ts,
            decided_by=d.get("decided_by", actor),
        )

    def _on_results_notified(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        for sid in d["submission_ids"]:
            view = self.submissions.get(sid)
            if view is not None and view.decision is not None:
                view.decision.notified = True

    def _on_results_announced(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        self.announcements.append({"seq": seq, "ts": ts, "actor": actor, **d})
        for item in d.get("outcomes", ()):
            view = self.submissions.get(item["submission_id"])
            if view is not None and view.decision is not None:
                view.decision.announced_seq = seq

    def _on_access_denied(self, seq: int, ts: str, actor: str, d: dict[str, Any]) -> None:
        self.access_denials.append({"seq": seq, "ts": ts, **d})

    def _get(self, sid: str) -> SubmissionView:
        try:
            return self.submissions[sid]
        except KeyError:
            raise ValueError(f"未知报名号：{sid}") from None
