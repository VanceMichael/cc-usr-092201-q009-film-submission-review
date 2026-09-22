"""征集审定后台的端到端规则测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from review.errors import (
    AuditError,
    AuthorizationError,
    ConflictError,
    DuplicateSubmissionError,
    LifecycleError,
    SecrecyError,
    ValidationError,
)
from review.rules import default_rules
from review.service import FestivalService

H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64

FULL_DETAIL = {
    "dialogue_list": {"detail": {"language": "阿拉伯语"}},
    "rights_proof": {"detail": {"holders": ["甲公司", "乙制片厂"], "co_producers": ["乙制片厂"]}},
    "screening_authorization": {"detail": {"scope": ["review", "screening"], "granted_by": "甲公司"}},
    "premiere_statement": {"detail": {"status": "国际首映"}},
    "submission_declaration": {
        "detail": {"submitter": "甲公司", "accepted_rules_version": "2026.1"}
    },
    "credits": {"detail": {"directors": ["李某"], "cast": ["张某"]}},
    "tech_spec": {"detail": {"format": "DCP", "fps": 24}},
}


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-09-22T00:00:{self.n:02d}+00:00"


def make_service() -> FestivalService:
    return FestivalService(rules=default_rules(), clock=Clock())


def register_ready(svc: FestivalService, sid: str = "S001", **overrides) -> None:
    params = dict(
        title="丝路短片",
        submitter="甲公司",
        directors=["李某"],
        countries=["中国", "埃及"],
        section="短片竞赛",
        materials=FULL_DETAIL,
    )
    params.update(overrides)
    svc.register_submission(sid, **params)
    svc.upload_media(
        sid, "screener", file_id="F1", filename="silk.mov", sha256=H1,
        note="初剪", subtitle_languages=["中文"], origin_key="WORK-1",
    )


class AuditChainTest(unittest.TestCase):
    def test_every_change_is_an_append_event(self):
        svc = make_service()
        register_ready(svc)
        svc.request_supplement("S001", ["still"], reason="宣传缺剧照", actor="selection")
        svc.provide_material("S001", "still", detail={"count": 3})
        svc.withdraw("S001", reason="制片方退出")
        actions = [e.action for e in svc.log.events("S001")]
        self.assertIn("material_supplement_requested", actions)
        self.assertIn("material_provided", actions)
        self.assertIn("withdrawn", actions)
        svc.log.verify_chain()

    def test_tampered_history_is_detected_on_load(self):
        svc = make_service()
        register_ready(svc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.json"
            svc.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["events"][3]["data"]["title"] = "被篡改的片名"
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(AuditError):
                FestivalService.load(path)

    def test_replay_rebuilds_identical_state(self):
        svc = make_service()
        register_ready(svc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.json"
            svc.save(path)
            reloaded = FestivalService.load(path)
            self.assertEqual(
                svc.readiness_report("S001")["valid_files"],
                reloaded.readiness_report("S001")["valid_files"],
            )
            self.assertEqual(len(reloaded.log), len(svc.log))


class SubmissionAndMaterialsTest(unittest.TestCase):
    def test_required_fields_validated(self):
        svc = make_service()
        with self.assertRaises(ValidationError):
            svc.register_submission(
                "S001", title="", submitter="甲公司",
                directors=["李某"], countries=["中国"], section="短片竞赛",
            )
        with self.assertRaises(ValidationError):
            svc.register_submission(
                "S002", title="x", submitter="甲公司",
                directors=["李某"], countries=["亚特兰蒂斯"], section="短片竞赛",
            )
        with self.assertRaises(ValidationError):
            svc.register_submission(
                "S003", title="x", submitter="甲公司",
                directors=["李某"], countries=["中国"], section="不存在的单元",
            )

    def test_missing_list_and_status_flow(self):
        svc = make_service()
        svc.register_submission(
            "S001", title="丝路短片", submitter="甲公司",
            directors=["李某"], countries=["中国"], section="短片竞赛",
        )
        self.assertEqual(svc.derived_status("S001"), "已报名")
        keys = {m["key"] for m in svc.missing_materials("S001")}
        self.assertIn("screener", keys)
        self.assertIn("screening_authorization", keys)
        svc.request_supplement("S001", sorted(keys), actor="selection")
        self.assertEqual(svc.derived_status("S001"), "待补件")
        report = svc.readiness_report("S001")
        self.assertFalse(report["review_ready"])
        self.assertIn("screening_authorization", report["open_supplement_keys"])

    def test_partial_detail_still_missing(self):
        svc = make_service()
        svc.register_submission(
            "S001", title="t", submitter="甲公司",
            directors=["李某"], countries=["中国"], section="短片竞赛",
            materials={"submission_declaration": {"detail": {"submitter": "甲公司"}}},
        )
        missing = {m["reason"] for m in svc.missing_materials("S001")}
        self.assertTrue(any("accepted_rules_version" in r for r in missing))

    def test_duplicate_submitter_same_work_rejected_but_second_submitter_allowed(self):
        svc = make_service()
        register_ready(svc)
        with self.assertRaises(DuplicateSubmissionError):
            register_ready(svc, sid="S009")
        svc.register_submission(
            "S002", title="丝路短片", submitter="乙工作室",
            directors=["李某"], countries=["中国", "埃及"], section="金丝路奖竞赛",
        )  # 不报错：不同报名主体，用于同源识别

    def test_rights_countries_premiere_kept_as_separate_details(self):
        svc = make_service()
        register_ready(svc)
        pkg = svc.evidence_package("S001")
        self.assertEqual(pkg["materials"]["rights_proof"]["detail"]["co_producers"], ["乙制片厂"])
        self.assertEqual(pkg["materials"]["premiere_statement"]["detail"]["status"], "国际首映")

    def test_withdraw_blocks_further_changes(self):
        svc = make_service()
        register_ready(svc)
        svc.withdraw("S001", reason="退出")
        self.assertEqual(svc.derived_status("S001"), "已撤回")
        with self.assertRaises(LifecycleError):
            svc.provide_material("S001", "still", detail={})
        with self.assertRaises(LifecycleError):
            svc.change_section("S001", "全景展映")

    def test_section_change_is_append_only(self):
        svc = make_service()
        register_ready(svc)
        svc.change_section("S001", "全景展映", reason="片长不适合竞赛", actor="selection")
        view = svc.projector.submissions["S001"]
        self.assertEqual(view.section, "全景展映")
        self.assertEqual(view.section_history[0]["from"], "短片竞赛")
        self.assertEqual(view.section_history[0]["to"], "全景展映")


class MediaAndOriginTest(unittest.TestCase):
    def test_replace_keeps_old_version_and_audit_reason(self):
        svc = make_service()
        register_ready(svc)
        svc.replace_media(
            "S001", "screener", old_file_id="F1", file_id="F2",
            filename="silk_dc.mov", sha256=H2, note="导演剪辑版",
            subtitle_languages=["中文", "阿拉伯语"], reason="应制片方要求换版",
        )
        report = svc.readiness_report("S001")
        self.assertEqual(report["valid_files"][0]["file_id"], "F2")
        versions = svc.evidence_package("S001")["all_versions"]
        self.assertEqual([(v["file_id"], v["active"]) for v in versions], [("F1", False), ("F2", True)])
        replace_event = [e for e in svc.log.events("S001") if e.action == "media_version_replaced"][0]
        self.assertEqual(replace_event.data["reason"], "应制片方要求换版")

    def test_identical_hash_rejected_within_submission(self):
        svc = make_service()
        register_ready(svc)
        with self.assertRaises(DuplicateSubmissionError):
            svc.upload_media(
                "S001", "screener", file_id="F1X", filename="silk.mov", sha256=H1,
            )

    def test_same_origin_groups_directors_cut_subtitles_submitters(self):
        svc = make_service()
        register_ready(svc)
        # 第二个报名主体：同作品导演剪辑版 + 不同字幕
        svc.register_submission(
            "S002", title="丝路短片", submitter="乙工作室",
            directors=["李某"], countries=["中国", "埃及"], section="金丝路奖竞赛",
        )
        svc.upload_media(
            "S002", "screener", file_id="F2", filename="silk_dc.mov", sha256=H2,
            note="导演剪辑版", subtitle_languages=["中文", "阿拉伯语"], origin_key="WORK-1",
        )
        # 同一文件被另一报名主体再次登记（如代理机构重复报名）：靠摘要聚类
        svc.register_submission(
            "S003", title="丝路短片", submitter="丙代理",
            directors=["李某"], countries=["中国", "埃及"], section="全景展映",
        )
        svc.upload_media(
            "S003", "screener", file_id="F3", filename="copy.mov", sha256=H1,
            note="重复报名拷贝", subtitle_languages=["中文"], origin_key="WORK-1",
        )
        groups = svc.same_origin_groups()
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["multiple_submitters"], ["丙代理", "乙工作室", "甲公司"])
        notes = {v["note"] for v in group["versions"]}
        self.assertIn("导演剪辑版", notes)
        self.assertIn("重复报名拷贝", notes)
        self.assertIn("文件摘要相同", group["reason"])


class ConflictAndAccessTest(unittest.TestCase):
    def test_conflict_declared_before_assignment_blocks(self):
        svc = make_service()
        register_ready(svc)
        svc.declare_conflict("J1", "S001", reason="J1 为导演亲属", actor="J1")
        with self.assertRaises(ConflictError):
            svc.assign_judges("S001", ["J1", "J2"])
        # 拦截本身留痕
        blocked = [e for e in svc.log.events("S001") if e.action == "assignment_blocked"]
        self.assertEqual(blocked[0].data["blocking"][0]["juror_id"], "J1")
        # 去掉回避对象后可分配
        svc.assign_judges("S001", ["J2", "J3"])
        self.assertEqual(svc.derived_status("S001"), "评审中")

    def test_cannot_assign_when_missing_materials(self):
        svc = make_service()
        svc.register_submission(
            "S001", title="t", submitter="甲公司",
            directors=["李某"], countries=["中国"], section="短片竞赛",
        )
        with self.assertRaises(LifecycleError):
            svc.assign_judges("S001", ["J2"])

    def test_judge_access_requires_assignment_authorization_and_valid_file(self):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J2"])
        # 未分配评委
        with self.assertRaises(AuthorizationError):
            svc.access_media("J9", "S001")
        # 已分配评委拿到获授权的初筛样片
        token = svc.access_media("J2", "S001")
        self.assertEqual(token["sha256"], H1)
        # 展映文件需要 screening 授权范围
        svc.upload_media(
            "S001", "screening_file", file_id="D1", filename="silk.dcp",
            sha256=H3, note="DCP",
        )
        dcp = svc.access_media("J2", "S001", kind="screening_file")
        self.assertEqual(dcp["file_id"], "D1")
        # 越权尝试记入审计
        denials = svc.projector.access_denials
        self.assertTrue(any(d["juror_id"] == "J9" for d in denials))

    def test_access_denied_without_screening_scope(self):
        svc = make_service()
        detail = dict(FULL_DETAIL)
        detail["screening_authorization"] = {"detail": {"scope": ["review"]}}
        register_ready(svc, materials=detail)
        svc.upload_media(
            "S001", "screening_file", file_id="D1", filename="silk.dcp", sha256=H3,
        )
        svc.assign_judges("S001", ["J2"])
        with self.assertRaises(AuthorizationError):
            svc.access_media("J2", "S001", kind="screening_file")
        # 初筛样片仍可接触
        self.assertEqual(svc.access_media("J2", "S001")["file_id"], "F1")

    def test_access_denied_after_withdrawal(self):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J2"])
        svc.withdraw("S001", reason="退出")
        with self.assertRaises(AuthorizationError):
            svc.access_media("J2", "S001")


class ScoringReviewDecisionTest(unittest.TestCase):
    def _decided_film(self, outcome="未入选"):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J2", "J3"])
        svc.submit_score("J2", "S001", 8.0, comment="镜头出色")
        svc.submit_score("J3", "S001", 7.5, comment="节奏略慢")
        svc.record_decision("S001", outcome, rationale="综合名额与评分", actor="jury-president")
        return svc

    def test_score_correction_is_append_only(self):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J3"])
        svc.submit_score("J3", "S001", 7.5)
        svc.correct_score("J3", "S001", 8.5, reason="评分表误填")
        view = svc.projector.submissions["S001"]
        self.assertEqual(view.current_score("J3").value, 8.5)
        score_events = [e for e in svc.log.events("S001") if e.action == "score_submitted"]
        self.assertEqual(len(score_events), 2)
        self.assertEqual(score_events[1].data["supersedes_seq"], score_events[0].seq)

    def test_score_range_and_assignment_guarded(self):
        svc = make_service()
        register_ready(svc)
        with self.assertRaises(AuthorizationError):
            svc.submit_score("J9", "S001", 9.0)
        svc.assign_judges("S001", ["J2"])
        with self.assertRaises(ValidationError):
            svc.submit_score("J2", "S001", 11.0)

    def test_decision_requires_all_independent_scores(self):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J2", "J3"])
        svc.submit_score("J2", "S001", 8.0)
        with self.assertRaises(LifecycleError):
            svc.record_decision("S001", "入选")

    def test_unselected_isolated_until_notification(self):
        svc = self._decided_film("未入选")
        # 评委/公众在通知前不可见
        with self.assertRaises(SecrecyError):
            svc.get_decision("S001", role="juror")
        with self.assertRaises(SecrecyError):
            svc.get_decision("S001", role="public")
        # 组委会可见
        self.assertEqual(svc.get_decision("S001", role="organizer")["outcome"], "未入选")
        # 未通知不能公布
        with self.assertRaises(SecrecyError):
            svc.announce("第12届", [{"submission_id": "S001"}])
        svc.notify_results(["S001"])
        svc.announce("第12届", [{"submission_id": "S001"}])
        self.assertEqual(svc.get_decision("S001", role="public")["outcome"], "未入选")
        self.assertEqual(svc.derived_status("S001"), "已审定")

    def test_review_overturn_before_notification(self):
        svc = self._decided_film("未入选")
        # 已审定后直接改分/改判被拒
        with self.assertRaises(LifecycleError):
            svc.correct_score("J3", "S001", 9.5, reason="复议")
        with self.assertRaises(LifecycleError):
            svc.record_decision("S001", "入选")
        # 复核：发起 → 结论推翻 → 改判，全过程留痕
        svc.request_review("S001", reason="发现计分错误", actor="J3")
        with self.assertRaises(LifecycleError):
            svc.record_decision("S001", "入选")  # 复核未结论
        svc.resolve_review("S001", conclusion="推翻原结论", reason="计分错误属实", actor="chair")
        svc.record_decision("S001", "入选", awards=["金丝路最佳短片"], rationale="复核后改判")
        pkg = svc.evidence_package("S001")
        self.assertEqual(pkg["decision"]["outcome"], "入选")
        self.assertEqual([r["kind"] for r in pkg["review_process"]], ["request", "resolution"])

    def test_award_only_in_award_sections(self):
        svc = make_service()
        register_ready(svc, section="全景展映")
        svc.assign_judges("S001", ["J2"])
        svc.submit_score("J2", "S001", 9.0)
        with self.assertRaises(LifecycleError):
            svc.record_decision("S001", "入选", awards=["某奖"])

    def test_open_review_blocks_decision_and_announcement(self):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J2"])
        svc.submit_score("J2", "S001", 8.0)
        svc.request_review("S001", reason="争议", actor="J2")
        with self.assertRaises(LifecycleError):
            svc.record_decision("S001", "入选")


class RulesVersionTest(unittest.TestCase):
    def test_conclusion_points_back_to_applicable_rules(self):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J2"])
        svc.submit_score("J2", "S001", 8.0)
        svc.record_decision("S001", "入选")
        pkg = svc.evidence_package("S001")
        self.assertEqual(pkg["applicable_rules"]["version"], "2026.1")
        self.assertIn("screener", pkg["applicable_rules"]["required_materials"])

    def test_new_rules_version_does_not_rewrite_old_conclusion(self):
        svc = make_service()
        register_ready(svc)
        svc.assign_judges("S001", ["J2"])
        svc.submit_score("J2", "S001", 8.0)
        svc.record_decision("S001", "入选")
        new_rules = default_rules(version="2026.2", sections={"金丝路奖竞赛", "短片竞赛", "全景展映", "主宾国展映", "新设单元"})
        svc.publish_rules(new_rules)
        # 旧结论仍回查到 2026.1
        self.assertEqual(svc.evidence_package("S001")["applicable_rules"]["version"], "2026.1")


if __name__ == "__main__":
    unittest.main()
