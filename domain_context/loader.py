"""读取并检查共享领域资料。"""

import json
from collections import Counter
from pathlib import Path

REQUIRED = {
    "domain",
    "version",
    "sample_id",
    "record_types",
    "workflow_states",
    "facts",
    "policies",
    "sample",
}

# 仅允许以追加记录体现的变更类型。
APPEND_EVENT_TYPES = {"补件", "撤回", "换版", "调整单元", "更正评分"}


def load_domain(path: Path) -> dict:
    """返回字段完整且满足征集审定治理约定的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_domain(value)
    return value


def validate_domain(value: dict) -> None:
    """对样例执行结构与治理规则校验，不合规时抛出 ValueError。"""
    if not REQUIRED.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if value["version"] < 2:
        raise ValueError("领域资料版本过旧，需要版本 2 或以上")
    if len(value["record_types"]) < 3 or len(value["workflow_states"]) < 3:
        raise ValueError("领域资料内容不完整")

    policies = value["policies"]
    missing_policy = {
        "append_only_events",
        "dedup",
        "access",
        "recusal",
        "isolation",
        "traceability",
    } - set(policies)
    if missing_policy:
        raise ValueError(f"治理约定缺失：{sorted(missing_policy)}")

    sample = value["sample"]
    _validate_media(sample)
    _validate_authorization(sample)
    _validate_recusals(sample)
    _validate_append_records(sample, set(policies["append_only_events"]))
    _validate_decision(sample)


def _validate_media(sample: dict) -> None:
    versions = sample["media_versions"]
    if not versions:
        raise ValueError("至少需要一个媒体版本")
    ids = [v["id"] for v in versions]
    if len(set(ids)) != len(ids):
        raise ValueError("媒体版本标识必须唯一，同源重复上传各自保留")
    # 每次上传按收件序号连续登记，丢弃任何一次都会破坏连续性。
    seqs = sorted(v["upload_seq"] for v in versions)
    if seqs != list(range(1, len(versions) + 1)):
        raise ValueError("媒体版本收件序号必须从 1 连续，各次上传均须保留")
    digests = [v["file_digest"] for v in versions]
    if any(not d.startswith("sha256:") for d in digests):
        raise ValueError("媒体版本文件摘要必须为 sha256 摘要")
    # 同源重复上传（摘要一致）必须全部保留，不能只留最后一次。
    for digest, count in Counter(digests).items():
        if count < 2:
            continue
        kept = [v for v in versions if v["file_digest"] == digest]
        if len(kept) != count:
            raise ValueError("同源重复上传必须分别保留作为参评依据")


def _validate_authorization(sample: dict) -> None:
    auth = sample["screening_authorization"]
    valid_media = {v["id"] for v in sample["media_versions"] if v["state"] == "有效"}
    if not set(auth["media"]).issubset(valid_media):
        raise ValueError("放映授权只能覆盖有效影片文件")
    # 评委可审文件必须一目了然，且只能来自获授权内容。
    reviewable = set(sample["reviewable_files"])
    if auth["granted"] and not reviewable.issubset(set(auth["media"])):
        raise ValueError("评委只能接触获授权内容")
    if not reviewable.issubset(valid_media):
        raise ValueError("可审文件必须是有效影片文件")


def _validate_recusals(sample: dict) -> None:
    recused = set()
    for recusal in sample["recusals"]:
        if recusal.get("effective_before_assignment") is not True:
            raise ValueError("回避关系必须在分配影片前生效")
        recused.add(recusal["judge"])
    # 回避在分配前生效：被回避评委不得出现在评分人之中。
    scoring_judges = {s["judge"] for s in sample.get("scores", [])}
    overlap = recused & scoring_judges
    if overlap:
        raise ValueError(f"被回避评委不得参与评分：{sorted(overlap)}")


def _validate_append_records(sample: dict, allowed_types: set) -> None:
    records = sample["append_records"]
    seqs = [r["seq"] for r in records]
    if seqs != list(range(1, len(records) + 1)):
        raise ValueError("追加记录序号必须从 1 连续递增，历史不得改写")
    for record in records:
        if record["type"] not in APPEND_EVENT_TYPES or record["type"] not in allowed_types:
            raise ValueError(f"不允许的追加记录类型：{record['type']}")
    # 更正评分只能通过追加记录表达，并指向独立评分。
    score_ids = {s["id"] for s in sample["scores"] if s.get("independent") is True}
    for record in records:
        if record["type"] == "更正评分":
            if record.get("score_id") not in score_ids:
                raise ValueError("更正评分必须指向已有评分")
            if record.get("from") == record.get("to"):
                raise ValueError("更正评分前后取值不能相同")


def _validate_decision(sample: dict) -> None:
    decision = sample["decision"]
    valid_media = {v["id"] for v in sample["media_versions"] if v["state"] == "有效"}
    if not set(decision["valid_media"]).issubset(valid_media):
        raise ValueError("审定结论引用的影片文件必须有效")
    scores = sample["scores"]
    independent_scores = {s["id"] for s in scores if s.get("independent") is True}
    if len(independent_scores) != len(scores):
        raise ValueError("评委评分必须独立作出")
    if not set(decision["score_ids"]).issubset(independent_scores):
        raise ValueError("审定结论必须可回查独立评分")
    if not decision["score_ids"]:
        raise ValueError("审定结论必须存在独立评分")
    if not decision.get("regulation") or not decision["review_process"]:
        raise ValueError("审定结论必须可回查适用章程与复核过程")
    # 未入选结果在正式通知前保持隔离：通知发出前须有隔离时点。
    if decision["result"] == "未入选" and not decision.get("isolated_until"):
        raise ValueError("未入选结果在正式通知前必须保持隔离")
