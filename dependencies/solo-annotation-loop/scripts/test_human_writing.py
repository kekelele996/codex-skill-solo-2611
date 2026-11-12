#!/usr/bin/env python3
"""五维评语证据门禁的回归测试。"""
from __future__ import annotations

import unittest
import importlib.util
import json
import tempfile
from pathlib import Path

from human_writing import (
    comment_evidence_issues,
    non_perfect_deduction_issues,
    validate_comment_consistency,
)


VALIDATE_COMMENTS_PATH = Path(__file__).with_name("validate-comments.py")
_spec = importlib.util.spec_from_file_location("validate_comments", VALIDATE_COMMENTS_PATH)
assert _spec and _spec.loader
_validate_comments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validate_comments)


class CommentEvidenceTests(unittest.TestCase):
    def test_abstract_comment_is_rejected(self) -> None:
        issues = comment_evidence_issues("验证通过，整体表现符合要求。")
        self.assertTrue(any("具体操作步骤或工具调用" in issue for issue in issues))
        self.assertTrue(any("文件名、函数名或命令引用" in issue for issue in issues))

    def test_step_without_artifact_is_rejected(self) -> None:
        issues = comment_evidence_issues("第三步执行并检查了结果，但没有记录可回指对象。")
        self.assertFalse(any("具体操作步骤或工具调用" in issue for issue in issues))
        self.assertTrue(any("文件名、函数名或命令引用" in issue for issue in issues))

    def test_file_name_is_accepted(self) -> None:
        self.assertEqual(
            comment_evidence_issues("第三步读取 server.py 并检查返回结果。"),
            [],
        )

    def test_function_name_is_accepted(self) -> None:
        self.assertEqual(
            comment_evidence_issues("调用 createReading 函数并验证返回结果。"),
            [],
        )

    def test_executable_command_is_accepted(self) -> None:
        self.assertEqual(
            comment_evidence_issues("运行 go test ./... 检查回归结果。"),
            [],
        )

    def test_validate_comments_wires_evidence_gate(self) -> None:
        comments = {key: "验证通过，整体表现符合要求。" for key in _validate_comments.DIMS}
        errors = _validate_comments.validate(comments)
        self.assertTrue(any("delivery缺少具体操作步骤或工具调用" in error for error in errors))
        self.assertTrue(any("execution缺少文件名、函数名或命令引用" in error for error in errors))

    def test_non_perfect_requires_negative_judgment_and_consequence(self) -> None:
        comments = {
            key: "运行 go test ./... 全部通过，接口调用和页面展示正常。" for key in _validate_comments.DIMS
        }
        scores = {key: 4 for key in _validate_comments.DIMS}
        issues = non_perfect_deduction_issues(comments, scores)
        self.assertEqual(len(issues), len(_validate_comments.DIMS) * 2)
        self.assertTrue(any("缺少明确负面判断" in issue for issue in issues))
        self.assertTrue(any("缺少客观后果" in issue for issue in issues))

    def test_non_perfect_accepts_location_negative_and_consequence(self) -> None:
        comments = {
            key: "运行 go test ./... 时 TestRecovery 未覆盖失败恢复路径，导致回归缺口残留。"
            for key in _validate_comments.DIMS
        }
        self.assertEqual(
            non_perfect_deduction_issues(comments, {key: 4 for key in _validate_comments.DIMS}),
            [],
        )

    def test_comment_consistency_requires_negative_evidence_for_non_perfect_score(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "traces").mkdir()
            trace = root / "traces/run.jsonl"
            trace.write_text('{"result":"go test passed"}\n', encoding="utf-8")
            evidence = root / "checks/comment-evidence/r01.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "dimensions": {
                            key: [
                                {
                                    "claim": "go test 通过",
                                    "kind": "trace_quote",
                                    "path": "traces/run.jsonl",
                                    "lineStart": 1,
                                    "lineEnd": 1,
                                    "quote": "go test passed",
                                    "polarity": "positive",
                                }
                            ]
                            for key in _validate_comments.DIMS
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            comments = {key: "go test 通过。" for key in _validate_comments.DIMS}
            scores = {key: 4 for key in _validate_comments.DIMS}
            errors = validate_comment_consistency(
                comments, evidence, root, scores=scores
            )
            self.assertTrue(
                any("没有负面或混合证据" in error for error in errors)
            )

    def test_comment_consistency_rejects_missing_evidence_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            comments = {key: "运行 go test ./... 并通过。" for key in _validate_comments.DIMS}
            errors = validate_comment_consistency(
                comments, root / "checks/comment-evidence/r01.json", root
            )
            self.assertTrue(any("缺少评语证据索引" in error for error in errors))

    def test_comment_consistency_accepts_exact_trace_quote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "traces").mkdir()
            trace = root / "traces/run.jsonl"
            trace.write_text('{"result":"go test passed"}\n', encoding="utf-8")
            evidence = root / "checks/comment-evidence/r01.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "dimensions": {
                            key: [
                                {
                                    "claim": "go test 通过",
                                    "kind": "trace_quote",
                                    "path": "traces/run.jsonl",
                                    "lineStart": 1,
                                    "lineEnd": 1,
                                    "quote": "go test passed",
                                    "polarity": "positive",
                                }
                            ]
                            for key in _validate_comments.DIMS
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            comments = {key: "go test 通过。" for key in _validate_comments.DIMS}
            self.assertEqual(
                validate_comment_consistency(comments, evidence, root), []
            )

    def test_comment_consistency_rejects_opposite_polarity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "traces").mkdir()
            trace = root / "traces/run.jsonl"
            trace.write_text('{"result":"go test failed"}\n', encoding="utf-8")
            evidence = root / "checks/comment-evidence/r01.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "dimensions": {
                            key: [
                                {
                                    "claim": "go test 通过",
                                    "kind": "trace_quote",
                                    "path": "traces/run.jsonl",
                                    "lineStart": 1,
                                    "lineEnd": 1,
                                    "quote": "go test failed",
                                    "polarity": "positive",
                                }
                            ]
                            for key in _validate_comments.DIMS
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            comments = {key: "go test 通过。" for key in _validate_comments.DIMS}
            errors = validate_comment_consistency(comments, evidence, root)
            self.assertTrue(any("引用了失败原文" in error for error in errors))

    def test_comment_consistency_rejects_unbound_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "traces").mkdir()
            trace = root / "traces/run.jsonl"
            trace.write_text('{"result":"go test passed"}\n', encoding="utf-8")
            evidence = root / "checks/comment-evidence/r01.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "dimensions": {
                            key: [
                                {
                                    "claim": "go test 通过",
                                    "kind": "trace_quote",
                                    "path": "traces/run.jsonl",
                                    "lineStart": 1,
                                    "lineEnd": 1,
                                    "quote": "go test passed",
                                    "polarity": "positive",
                                }
                            ]
                            for key in _validate_comments.DIMS
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            comments = {
                key: "go test 通过。部署也已成功。" for key in _validate_comments.DIMS
            }
            errors = validate_comment_consistency(comments, evidence, root)
            self.assertTrue(any("有事实句没有绑定证据" in error for error in errors))

    def test_comment_consistency_accepts_complete_source_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source").mkdir()
            source = root / "source/migrate_test.go"
            source.write_text(
                "package database\nfunc TestFresh(t *testing.T) {}\nfunc TestUpgrade(t *testing.T) {}\n",
                encoding="utf-8",
            )
            evidence = root / "checks/comment-evidence/r01.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "dimensions": {
                            key: [
                                {
                                    "claim": "迁移恢复路径未进入用例",
                                    "kind": "source_scope",
                                    "path": "source/migrate_test.go",
                                    "symbols": ["TestFresh", "TestUpgrade"],
                                    "complete": True,
                                    "polarity": "negative",
                                }
                            ]
                            for key in _validate_comments.DIMS
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            comments = {key: "迁移恢复路径未进入用例。" for key in _validate_comments.DIMS}
            self.assertEqual(
                validate_comment_consistency(comments, evidence, root), []
            )

    def test_comment_consistency_rejects_false_complete_source_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source").mkdir()
            source = root / "source/migrate_test.go"
            source.write_text(
                "package database\nfunc TestFresh(t *testing.T) {}\nfunc TestUpgrade(t *testing.T) {}\nfunc TestRecovery(t *testing.T) {}\n",
                encoding="utf-8",
            )
            evidence = root / "checks/comment-evidence/r01.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "dimensions": {
                            key: [
                                {
                                    "claim": "迁移恢复路径未进入用例",
                                    "kind": "source_scope",
                                    "path": "source/migrate_test.go",
                                    "symbols": ["TestFresh", "TestUpgrade"],
                                    "complete": True,
                                    "polarity": "negative",
                                }
                            ]
                            for key in _validate_comments.DIMS
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            comments = {key: "迁移恢复路径未进入用例。" for key in _validate_comments.DIMS}
            errors = validate_comment_consistency(comments, evidence, root)
            self.assertTrue(any("覆盖清单不一致" in error for error in errors))

    def test_comment_consistency_rejects_cross_round_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "traces").mkdir()
            trace = root / "traces/run.jsonl"
            trace.write_text('{"result":"go test passed"}\n', encoding="utf-8")
            evidence = root / "checks/comment-evidence/r02.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "round": 1,
                        "sessionId": "s1",
                        "promptId": "p1",
                        "dimensions": {
                            key: [
                                {
                                    "claim": "go test 通过",
                                    "kind": "trace_quote",
                                    "path": "traces/run.jsonl",
                                    "lineStart": 1,
                                    "lineEnd": 1,
                                    "quote": "go test passed",
                                    "polarity": "positive",
                                }
                            ]
                            for key in _validate_comments.DIMS
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            comments = {key: "go test 通过。" for key in _validate_comments.DIMS}
            errors = validate_comment_consistency(
                comments, evidence, root, 2, "s1", "p1"
            )
            self.assertTrue(any("轮次不一致" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
