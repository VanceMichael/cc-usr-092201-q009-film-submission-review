import unittest
from pathlib import Path

from domain_context.loader import load_domain, validate_domain

FIXTURE = Path("fixtures/domain.json")


def sample_value() -> dict:
    return load_domain(FIXTURE)


class DomainFixtureTest(unittest.TestCase):
    def test_fixture_is_complete(self):
        value = sample_value()
        self.assertEqual(value["domain"], "film-submission-review")
        self.assertGreaterEqual(value["version"], 2)
        self.assertGreaterEqual(len(value["facts"]), 2)


class SameSourceVersionsTest(unittest.TestCase):
    def test_duplicate_uploads_are_both_kept(self):
        value = sample_value()
        digests = [v["file_digest"] for v in value["sample"]["media_versions"]]
        duplicated = {d for d in digests if digests.count(d) > 1}
        self.assertTrue(duplicated, "样例应包含同源重复上传")
        # 不同报名主体的重复上传各自保留，不被最后一次覆盖。
        for digest in duplicated:
            uploads = [v for v in value["sample"]["media_versions"] if v["file_digest"] == digest]
            self.assertGreaterEqual(len({u["submitted_by"] for u in uploads}), 2)

    def test_dropping_a_duplicate_fails(self):
        value = sample_value()
        value["sample"]["media_versions"].pop(1)
        with self.assertRaises(ValueError):
            validate_domain(value)


class SeparateFactsTest(unittest.TestCase):
    def test_rights_premiere_subtitles_and_specs_are_separate(self):
        sample = sample_value()["sample"]
        self.assertIn("主创权利", [r["kind"] for r in sample["rights"]])
        self.assertIn("联合制作权利", [r["kind"] for r in sample["rights"]])
        self.assertTrue(sample["submission"]["premiere_status"])
        self.assertTrue(sample["subtitle_languages"])
        self.assertTrue(sample["technical_spec"])
        self.assertTrue(sample["submission"]["declarations"])

    def test_reviewable_and_missing_items_are_listed(self):
        sample = sample_value()["sample"]
        # 当前可审文件与缺件都明确列出，一目了然。
        self.assertTrue(sample["reviewable_files"])
        self.assertIn("联合制作权利证明", sample["missing_items"])


class AuthorizationTest(unittest.TestCase):
    def test_reviewable_files_stay_within_authorization(self):
        sample = sample_value()["sample"]
        authorized = set(sample["screening_authorization"]["media"])
        self.assertTrue(set(sample["reviewable_files"]).issubset(authorized))

    def test_unauthorized_file_cannot_be_reviewable(self):
        value = sample_value()
        # MV-0002 未获放映授权。
        value["sample"]["reviewable_files"].append("MV-0002")
        with self.assertRaises(ValueError):
            validate_domain(value)


class RecusalTest(unittest.TestCase):
    def test_recusal_effective_before_assignment(self):
        sample = sample_value()["sample"]
        self.assertTrue(sample["recusals"])
        self.assertTrue(all(r["effective_before_assignment"] for r in sample["recusals"]))

    def test_recused_judge_cannot_score(self):
        value = sample_value()
        recused = value["sample"]["recusals"][0]["judge"]
        value["sample"]["scores"].append(
            {"id": "SCR-0003", "judge": recused, "value": 7, "independent": True}
        )
        with self.assertRaises(ValueError):
            validate_domain(value)


class AppendRecordsTest(unittest.TestCase):
    def test_append_record_types_cover_all_change_kinds(self):
        value = sample_value()
        # 治理约定覆盖补件、撤回、换版、调整单元、更正评分五类变更。
        self.assertEqual(
            set(value["policies"]["append_only_events"]),
            {"补件", "撤回", "换版", "调整单元", "更正评分"},
        )
        types = {r["type"] for r in value["sample"]["append_records"]}
        self.assertTrue(types.issubset(set(value["policies"]["append_only_events"])))

    def test_seq_must_be_continuous(self):
        value = sample_value()
        value["sample"]["append_records"][-1]["seq"] = 9
        with self.assertRaises(ValueError):
            validate_domain(value)

    def test_score_correction_points_to_existing_score(self):
        value = sample_value()
        value["sample"]["append_records"][-1]["score_id"] = "SCR-404"
        with self.assertRaises(ValueError):
            validate_domain(value)


class DecisionTraceabilityTest(unittest.TestCase):
    def test_decision_traces_regulation_media_scores_and_review(self):
        decision = sample_value()["sample"]["decision"]
        self.assertTrue(decision["regulation"])
        self.assertTrue(decision["valid_media"])
        self.assertTrue(decision["score_ids"])
        self.assertIn("复核", decision["review_process"])

    def test_scores_are_independent(self):
        value = sample_value()
        value["sample"]["scores"][0]["independent"] = False
        with self.assertRaises(ValueError):
            validate_domain(value)

    def test_unselected_result_is_isolated_before_notification(self):
        decision = sample_value()["sample"]["decision"]
        self.assertEqual(decision["result"], "未入选")
        self.assertTrue(decision["isolated_until"])

    def test_unselected_without_isolation_fails(self):
        value = sample_value()
        del value["sample"]["decision"]["isolated_until"]
        with self.assertRaises(ValueError):
            validate_domain(value)


if __name__ == "__main__":
    unittest.main()
