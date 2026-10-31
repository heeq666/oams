"""`oams plugin check` 的另一半：签字人是不是库里真有的那个人。

离线校验能证明的东西到此为止——字段、状态、模块、容器都验完了，唯独「谁签的」
这一条只有数据库答得上来。这一组用例钉住三件事：

* 给了在职成员工号名单，手写一个查无此人的名字**当场被拒**；
* 不给名单（离线），形状照样验，但报告里必须写明**这条没验**，不许「OK 通过」了事；
* `--json` 也带上这个事实，CI 和编辑器钩子不用去解析人话。

用 `examples/production-dept.yaml` 当底稿：它是能过校验的真文件，改成公开页 + 一个
手写的章，就是「作者自己给自己批」的那种输入。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from oams import plugin_cli

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "production-dept.yaml"


def _signed(tmp_path: Path, reviewer: str) -> Path:
    """示例文件改成一个「公开页 + 手写章」的版本。"""
    descriptor = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    descriptor["meta"]["visibility"] = "public"
    descriptor["meta"]["review"] = {"status": "approved", "reviewed_by": reviewer,
                                    "reviewed_at": "2026-10-31T09:00:00Z"}
    target = tmp_path / f"signed-{abs(hash(reviewer))}.json"
    target.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def test_a_name_nobody_recognises_is_refused_when_the_list_is_known(tmp_path):
    path = _signed(tmp_path, "王二")
    assert plugin_cli.check_file(path) == [], "离线只验形状，这一条它查不了"

    problems = plugin_cli.check_file(path, known_employee_codes={"DESIGN001", "ADMIN001"})
    assert len(problems) == 1
    assert "must name an active member" in problems[0]
    assert "王二" in problems[0]


def test_a_signature_by_a_real_employee_passes_both_ways(tmp_path):
    path = _signed(tmp_path, "DESIGN001")
    assert plugin_cli.check_file(path) == []
    assert plugin_cli.check_file(path, known_employee_codes={"DESIGN001"}) == []


def test_the_offline_report_says_what_it_did_not_check(tmp_path):
    """「没验」和「验过没问题」必须长得不一样，否则离线 OK 会被读成盖章。"""
    path = _signed(tmp_path, "王二")
    offline = plugin_cli.render_report(path, [], signer_verified=False)
    assert "没验的一条" in offline
    assert "--database" in offline

    online = plugin_cli.render_report(path, [], signer_verified=True)
    assert "没验" not in online


def test_json_output_carries_that_the_signer_was_not_verified(tmp_path, capsys):
    path = _signed(tmp_path, "王二")
    assert plugin_cli.main(["check", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["signer_verified"] is False


@pytest.mark.parametrize("verb", ["check", "preview"])
def test_the_database_flag_exists_where_it_makes_sense(verb, tmp_path, capsys):
    """`--database` 只在能验人的两个子命令上出现；upgrade 是纯转换，不需要。"""
    import argparse

    with pytest.raises(SystemExit) as caught:
        plugin_cli.main([verb, "--help"])
    assert caught.value.code == 0
    helptext = capsys.readouterr().out
    assert "--database" in helptext
