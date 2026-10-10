"""受限数值公式的解析与 Decimal 求值。"""

from __future__ import annotations

import ast
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
import time
from typing import Any

from backend.experiments.errors import ExperimentError


MAX_FORMULA_LENGTH = 1000
MAX_FORMULA_NODES = 100
MAX_FORMULA_DEPTH = 20
DECIMAL_PRECISION = 34
MAX_FORMULA_SECONDS = 0.05
ALLOWED_FUNCTIONS = {"abs", "min", "max", "coalesce"}
ALLOWED_BINARY_OPERATORS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
ALLOWED_UNARY_OPERATORS = (ast.UAdd, ast.USub)


class SafeFormula:
  def __init__(self, expression: str, allowed_names: set[str]) -> None:
    if len(expression) > MAX_FORMULA_LENGTH:
      raise _formula_error("公式长度超过 1000 个字符")
    try:
      tree = ast.parse(expression, mode="eval")
    except (RecursionError, SyntaxError, ValueError) as error:
      raise _formula_error("公式语法无效") from error
    self._validate(tree, allowed_names)
    self._tree = tree
    self.names = frozenset(
      node.id for node in ast.walk(tree)
      if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCTIONS
    )

  def evaluate(
    self,
    values: dict[str, Decimal | int | None],
    precision: int,
  ) -> Decimal | None:
    with localcontext() as context:
      context.prec = DECIMAL_PRECISION
      try:
        deadline = time.perf_counter() + MAX_FORMULA_SECONDS
        value = self._evaluate_node(self._tree.body, values, deadline)
        if time.perf_counter() > deadline:
          raise ExperimentError(
            "experiment.evaluation_formula_timeout",
            "评价公式计算超时",
            422,
          )
        if value is None:
          return None
        if not value.is_finite():
          raise _formula_non_finite()
        quantum = Decimal(1).scaleb(-precision)
        normalized = value.quantize(quantum)
      except (DivisionByZero, ZeroDivisionError) as error:
        raise ExperimentError(
          "experiment.evaluation_formula_division_by_zero",
          "评价公式发生除零",
          422,
        ) from error
      except (InvalidOperation, OverflowError) as error:
        raise _formula_non_finite() from error
    if not normalized.is_finite():
      raise _formula_non_finite()
    return normalized

  @classmethod
  def _validate(cls, tree: ast.Expression, allowed_names: set[str]) -> None:
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_FORMULA_NODES:
      raise _formula_error("公式 AST 节点超过 100 个")
    if cls._depth(tree) > MAX_FORMULA_DEPTH:
      raise _formula_error("公式嵌套深度超过 20 层")
    for node in nodes:
      cls._validate_node(node, allowed_names)

  @staticmethod
  def _validate_node(node: ast.AST, allowed_names: set[str]) -> None:
    if isinstance(node, (ast.Expression, ast.Load)):
      return
    if isinstance(node, ast.Constant):
      if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
        raise _formula_error("公式常量只能是有限数值")
      value = Decimal(str(node.value))
      if not value.is_finite():
        raise _formula_non_finite()
      return
    if isinstance(node, ast.Name):
      if node.id not in allowed_names and node.id not in ALLOWED_FUNCTIONS:
        raise _formula_error(f"公式引用了未知指标: {node.id}")
      return
    if isinstance(node, ast.BinOp):
      if not isinstance(node.op, ALLOWED_BINARY_OPERATORS):
        raise _formula_error("公式包含不允许的二元运算")
      return
    if isinstance(node, ast.UnaryOp):
      if not isinstance(node.op, ALLOWED_UNARY_OPERATORS):
        raise _formula_error("公式包含不允许的一元运算")
      return
    if isinstance(node, (*ALLOWED_BINARY_OPERATORS, *ALLOWED_UNARY_OPERATORS)):
      return
    if isinstance(node, ast.Call):
      if (
        not isinstance(node.func, ast.Name)
        or node.func.id not in ALLOWED_FUNCTIONS
        or node.keywords
      ):
        raise _formula_error("公式函数调用不在白名单中")
      if node.func.id == "abs" and len(node.args) != 1:
        raise _formula_error("abs 只接受一个参数")
      if node.func.id in {"min", "max", "coalesce"} and len(node.args) < 1:
        raise _formula_error(f"{node.func.id} 至少接受一个参数")
      return
    raise _formula_error(f"公式包含不允许的节点: {type(node).__name__}")

  @staticmethod
  def _depth(node: ast.AST) -> int:
    maximum = 0
    pending = [(node, 1)]
    while pending:
      current, depth = pending.pop()
      maximum = max(maximum, depth)
      pending.extend((child, depth + 1) for child in ast.iter_child_nodes(current))
    return maximum

  def _evaluate_node(
    self,
    node: ast.AST,
    values: dict[str, Decimal | int | None],
    deadline: float,
  ) -> Decimal | None:
    if time.perf_counter() > deadline:
      raise ExperimentError(
        "experiment.evaluation_formula_timeout",
        "评价公式计算超时",
        422,
      )
    if isinstance(node, ast.Constant):
      return Decimal(str(node.value))
    if isinstance(node, ast.Name):
      value = values.get(node.id)
      return Decimal(value) if isinstance(value, int) else value
    if isinstance(node, ast.UnaryOp):
      operand = self._evaluate_node(node.operand, values, deadline)
      if operand is None:
        return None
      return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp):
      return self._evaluate_binary(node, values, deadline)
    if isinstance(node, ast.Call):
      return self._evaluate_call(node, values, deadline)
    raise _formula_error("公式运行节点无效")

  def _evaluate_binary(
    self,
    node: ast.BinOp,
    values: dict[str, Decimal | int | None],
    deadline: float,
  ) -> Decimal | None:
    left = self._evaluate_node(node.left, values, deadline)
    right = self._evaluate_node(node.right, values, deadline)
    if left is None or right is None:
      return None
    if isinstance(node.op, ast.Add):
      return left + right
    if isinstance(node.op, ast.Sub):
      return left - right
    if isinstance(node.op, ast.Mult):
      return left * right
    if right == 0:
      raise ExperimentError(
        "experiment.evaluation_formula_division_by_zero",
        "评价公式发生除零",
        422,
      )
    return left / right

  def _evaluate_call(
    self,
    node: ast.Call,
    values: dict[str, Decimal | int | None],
    deadline: float,
  ) -> Decimal | None:
    function_name = node.func.id  # type: ignore[union-attr]
    if function_name == "coalesce":
      for argument in node.args:
        value = self._evaluate_node(argument, values, deadline)
        if value is not None:
          return value
      return None
    arguments = [
      self._evaluate_node(argument, values, deadline) for argument in node.args
    ]
    if any(value is None for value in arguments):
      return None
    numeric = [value for value in arguments if value is not None]
    if function_name == "abs":
      return abs(numeric[0])
    if function_name == "min":
      return min(numeric)
    return max(numeric)


def _formula_error(message: str) -> ExperimentError:
  return ExperimentError("experiment.evaluation_formula_invalid", message, 422)


def _formula_non_finite() -> ExperimentError:
  return ExperimentError(
    "experiment.evaluation_formula_non_finite",
    "评价公式产生了非有限结果",
    422,
  )
