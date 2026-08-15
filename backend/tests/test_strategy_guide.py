from backend.app import strategy_guide


def test_strategy_guide_returns_the_fixed_manual() -> None:
  response = strategy_guide()
  content = response["content"]

  assert response["title"] == "策略编写指南"
  assert "# 玄龄回测系统使用手册" in content
  assert "on_bar(bar, state)" in content
  assert "### 1.1 先记住这 6 条" in content
  assert "### 1.6 结果、日期与错误处理" in content
