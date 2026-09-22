"""只追加的事件审计日志。

所有业务变更（补件、撤回、换版、调整单元、评分及更正、复核、通知……）
都以追加事件体现；当前状态由事件重放得到，绝不就地改写历史。
每条事件携带前一条事件的散列，形成哈希链，任何删改都能被发现。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from .errors import AuditError

GENESIS = "0" * 64


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def event_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + _canonical(payload)).hexdigest()


class Event:
    """一条不可变审计事件。"""

    __slots__ = ("seq", "ts", "actor", "action", "data", "prev_hash", "hash")

    def __init__(
        self,
        seq: int,
        ts: str,
        actor: str,
        action: str,
        data: dict[str, Any],
        prev_hash: str,
        digest: str,
    ) -> None:
        self.seq = seq
        self.ts = ts
        self.actor = actor
        self.action = action
        self.data = data
        self.prev_hash = prev_hash
        self.hash = digest

    @classmethod
    def create(
        cls, seq: int, ts: str, actor: str, action: str, data: dict[str, Any], prev_hash: str
    ) -> "Event":
        payload = {"seq": seq, "ts": ts, "actor": actor, "action": action, "data": data}
        return cls(seq, ts, actor, action, copy.deepcopy(data), prev_hash, event_hash(prev_hash, payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "data": copy.deepcopy(self.data),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            seq=raw["seq"],
            ts=raw["ts"],
            actor=raw["actor"],
            action=raw["action"],
            data=raw["data"],
            prev_hash=raw["prev_hash"],
            digest=raw["hash"],
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Event(#{self.seq} {self.action} by {self.actor})"


class EventLog:
    """顺序追加、可整体序列化重放的哈希链日志。"""

    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._events: list[Event] = []
        self._clock = clock or utc_now

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS

    def __len__(self) -> int:
        return len(self._events)

    def append(self, action: str, data: dict[str, Any], actor: str) -> Event:
        event = Event.create(
            seq=len(self._events) + 1,
            ts=self._clock(),
            actor=actor,
            action=action,
            data=data,
            prev_hash=self.head_hash,
        )
        self._events.append(event)
        return event

    def events(self, submission_id: str | None = None) -> Iterator[Event]:
        """按时间顺序产出事件；可只看某部影片的事件。"""
        for event in self._events:
            if submission_id is None or event.data.get("submission_id") == submission_id:
                yield event

    def verify_chain(self) -> None:
        """重算全部散列并核对序号，发现断裂即抛 AuditError。"""
        prev = GENESIS
        for index, event in enumerate(self._events, start=1):
            if event.seq != index:
                raise AuditError(f"事件序号断裂：位置 {index} 记录为 {event.seq}")
            if event.prev_hash != prev:
                raise AuditError(f"事件 #{event.seq} 前序散列不匹配")
            payload = {
                "seq": event.seq,
                "ts": event.ts,
                "actor": event.actor,
                "action": event.action,
                "data": event.data,
            }
            if event_hash(event.prev_hash, payload) != event.hash:
                raise AuditError(f"事件 #{event.seq} 内容散列不匹配（历史被改写）")
            prev = event.hash

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]

    @classmethod
    def from_list(cls, raw_events: list[dict[str, Any]], clock: Callable[[], str] | None = None) -> "EventLog":
        log = cls(clock=clock)
        log._events = [Event.from_dict(raw) for raw in raw_events]
        log.verify_chain()
        return log
