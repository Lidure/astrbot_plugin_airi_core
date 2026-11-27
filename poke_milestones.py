"""Poke milestone rule storage and action history."""

import hashlib
import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("开关必须是布尔值")
    return value


def _integer(value, label, minimum, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label}超出范围或不是整数")
    return value


def _validate_rule(fields, rule_id):
    allowed = {"name", "id", "target", "tolerance", "mute_minutes", "llm_enabled", "prompt", "enabled"}
    if set(fields) - allowed:
        raise ValueError("含有未知规则字段")
    name = fields.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
        raise ValueError("规则名称须为 1–40 字符")
    prompt = fields.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt.strip()) > 1000:
        raise ValueError("自定义提示须为 1000 字符以内的文本")
    return {
        "id": rule_id, "name": name.strip(),
        "target": _integer(fields.get("target"), "目标次数", 1, 1000000),
        "tolerance": _integer(fields.get("tolerance", 2), "随机容差", 0, 10000),
        "mute_minutes": _integer(fields.get("mute_minutes", 1), "禁言分钟数", 1, 43200),
        "llm_enabled": _boolean(fields.get("llm_enabled", False)),
        "prompt": prompt.strip(), "enabled": _boolean(fields.get("enabled", True)),
    }


def effective_threshold(rule, group_id, user_id, stats_day):
    seed = json.dumps([str(group_id), str(user_id), stats_day, rule["id"]],
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sample = int.from_bytes(hashlib.sha256(seed).digest(), "big")
    low = max(1, rule["target"] - rule["tolerance"])
    high = rule["target"] + rule["tolerance"]
    return low + sample % (high - low + 1)


class PokeMilestoneStore:
    """One store per plugin instance. Disk commits precede publishing in-memory changes."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = {"version": 1, "enabled": False, "rules": {}, "history": [],
                      "claim_day": "", "claims": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if not isinstance(loaded, dict) or any(type(loaded.get(k)) is not type(v)
                                                for k, v in self._data.items()) or loaded["version"] != 1:
            raise ValueError("无法识别里程碑数据格式")
        # Never activate partially corrupted rules or trust malformed claims.
        for rule_id, rule in loaded["rules"].items():
            if (not isinstance(rule, dict) or rule.get("id") != rule_id
                    or _validate_rule(rule, rule_id) != rule):
                raise ValueError("里程碑规则数据损坏")
        statuses = {"pending", "success", "failed", "unknown"}
        for key, row in loaded["claims"].items():
            parts = json.loads(key)
            if (not isinstance(parts, list) or len(parts) != 3
                    or not all(isinstance(part, str) for part in parts)
                    or not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or row.get("status") not in statuses):
                raise ValueError("里程碑去重数据损坏")
        if len(loaded["history"]) > 100:
            raise ValueError("里程碑执行记录超出范围")
        for row in loaded["history"]:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or row.get("status") not in statuses):
                raise ValueError("里程碑历史数据损坏")
        recovered = False
        for row in [*loaded["claims"].values(), *loaded["history"]]:
            if row["status"] == "pending":
                row.update(status="unknown", detail="上次执行中断，结果未确认")
                recovered = True
        self._data = loaded
        if recovered:
            self._commit(loaded)

    def _commit(self, data):
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, self.path)
        self._data = data

    def snapshot(self):
        with self._lock:
            return deepcopy({"enabled": self._data["enabled"], "rules": list(self._data["rules"].values()),
                             "history": self._data["history"]})

    @property
    def active(self):
        with self._lock:
            return self._data["enabled"] and any(r["enabled"] for r in self._data["rules"].values())

    def set_enabled(self, enabled):
        with self._lock:
            self._commit({**self._data, "enabled": _boolean(enabled)})

    def save_rule(self, fields):
        with self._lock:
            rules = self._data["rules"].copy()
            if "id" in fields:
                rule_id = fields["id"]
                if not isinstance(rule_id, str) or rule_id not in rules:
                    raise KeyError("规则不存在")
            else:
                rule_id = uuid.uuid4().hex
            rule = _validate_rule(fields, rule_id)
            rules[rule_id] = rule
            self._commit({**self._data, "rules": rules})
            return rule.copy()

    def delete_rule(self, rule_id):
        with self._lock:
            rules = self._data["rules"].copy()
            del rules[rule_id]
            self._commit({**self._data, "rules": rules})

    def set_rule_enabled(self, rule_id, enabled):
        with self._lock:
            self.save_rule({**self._data["rules"][rule_id], "enabled": _boolean(enabled)})

    @staticmethod
    def _key(rule, group_id, user_id):
        return json.dumps([str(group_id), str(user_id), rule["id"]], separators=(",", ":"))

    def _matches(self, rule, group_id, user_id, day, count):
        if not self._data["enabled"] or not rule["enabled"] or day < self._data["claim_day"]:
            return False
        threshold = effective_threshold(rule, group_id, user_id, day)
        if day == self._data["claim_day"]:
            previous = self._data["claims"].get(self._key(rule, group_id, user_id))
            if previous:
                return previous["status"] == "failed" and count >= threshold
        return count == threshold

    def matching_rules(self, group_id, user_id, stats_day, count):
        with self._lock:
            return [rule.copy() for rule in self._data["rules"].values()
                    if self._matches(rule, group_id, user_id, stats_day, count)]

    def claim(self, rule, group_id, user_id, stats_day, count):
        matches = self.claim_many([rule], group_id, user_id, stats_day, count)
        return matches[0][1] if matches else None

    def claim_many(self, candidates, group_id, user_id, stats_day, count):
        """Atomically reserve all still-matching rules before one OneBot action."""
        with self._lock:
            claims = self._data["claims"].copy() if stats_day == self._data["claim_day"] else {}
            actions, rows, seen = [], [], set()
            for rule in candidates:
                rule_id = rule["id"]
                if (rule_id in seen or self._data["rules"].get(rule_id) != rule
                        or not self._matches(rule, group_id, user_id, stats_day, count)):
                    continue
                seen.add(rule_id)
                action_id = uuid.uuid4().hex
                row = {"id": action_id, "rule_id": rule_id, "rule_name": rule["name"],
                       "group_id": str(group_id), "user_id": str(user_id), "stats_day": stats_day,
                       "count": count, "threshold": effective_threshold(rule, group_id, user_id, stats_day),
                       "mute_minutes": rule["mute_minutes"], "status": "pending", "detail": "",
                       "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
                claims[self._key(rule, group_id, user_id)] = {"id": action_id, "status": "pending"}
                actions.append((rule.copy(), action_id))
                rows.append(row)
            if actions:
                self._commit({**self._data, "claims": claims, "claim_day": stats_day,
                              "history": [*reversed(rows), *self._data["history"]][:100]})
            return actions

    def finish(self, action_id, status, detail=""):
        if status not in {"success", "failed", "unknown"}:
            raise ValueError("未知执行结果")
        with self._lock:
            data = deepcopy(self._data)
            for row in [*data["claims"].values(), *data["history"]]:
                if row["id"] == action_id and row["status"] == "pending":
                    row.update(status=status, detail=detail)
            self._commit(data)
