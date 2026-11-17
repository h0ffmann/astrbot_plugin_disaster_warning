"""
规则链执行器。
负责按既定顺序执行多个规则，并在出现首个拒绝结果时立即短路返回。
"""

from __future__ import annotations

from astrbot.api import logger

from .base_rule import RuleContext
from .intensity_rule import EarthquakeThresholdRule
from .keyword_rule import KeywordRule
from .local_rule import LocalIntensityRule
from .measurement_rule import MeasurementTypeRule
from .report_rule import ReportRule
from .rule_result import RuleDecision
from .source_rule import SourceEnabledRule
from .time_rule import EventTimeRule
from .tsunami_rule import TsunamiRule
from .typhoon_rule import TyphoonRule
from .weather_rule import WeatherRule


class RuleChain:
    """统一串联多个规则。

    规则链本身不关心具体业务，只负责维持执行顺序与短路语义。
    """

    def __init__(self, rules=None):
        # 接收并按传入顺序实例化过滤规则链表
        self.rules = list(rules or [])

    def evaluate(self, context: RuleContext) -> RuleDecision:
        """依次执行规则链中的每个规则。"""
        for rule in self.rules:
            # 依次执行各过滤子项评估
            decision = rule.evaluate(context)
            # 任一规则判定不放行时，执行短路保护，停止后续链式判断并立即向上层返回拒信结果
            if not decision.accepted:
                return decision
        # 全部通过，生成放行决策
        return RuleDecision.accept(reason="规则链通过")


def build_default_rule_chain() -> RuleChain:
    """构建默认推送规则链。

    默认顺序从基础有效性校验开始，再逐步进入来源、气象、台风、海啸、关键词、
    震动强度、报次与本地烈度判断。
    """
    # 顺次装配核心过滤逻辑规则实例
    return RuleChain(
        [
            EventTimeRule(),
            SourceEnabledRule(),
            MeasurementTypeRule(),
            WeatherRule(),
            TyphoonRule(),
            TsunamiRule(),
            KeywordRule(),
            EarthquakeThresholdRule(),
            ReportRule(),
            LocalIntensityRule(),
        ]
    )


# Extension rules: (rule, rule_name of the built-in rule before it, extension id).
_REGISTERED_RULES: list[tuple[object, str, str]] = []
_build_builtin_rule_chain = build_default_rule_chain


class _IsolatedRule:
    """规则链里的一条扩展规则。

    扩展规则自身求值抛出异常时不再中断整条规则链：一个坏的扩展规则
    不能拦下其他数据源的事件。异常记录为警告，规则按放行处理。
    """

    def __init__(self, rule):
        self._rule = rule
        # The wrapper keeps the rule_name of the rule. The chain uses the name
        # to find the anchor rule, and another extension can anchor after it.
        self.rule_name = str(getattr(rule, "rule_name", "") or "")

    def evaluate(self, context: RuleContext) -> RuleDecision:
        """Run the rule. An exception of the rule is isolated."""
        try:
            return self._rule.evaluate(context)
        except Exception as e:
            logger.warning(
                f"[灾害预警] 扩展规则 {self.rule_name!r} 求值异常，已放行: {type(e).__name__}: {e}"
            )
            return RuleDecision.accept(reason="扩展规则异常，已放行")


def register_rule(rule, *, after: str = "local_rule", owner: str) -> None:
    """Register an extension rule after the built-in rule with the name `after`.

    If no built-in rule has that name, the rule goes to the end.
    """
    _REGISTERED_RULES.append((rule, after, owner))


def unregister_rules(owner: str) -> None:
    """Remove all rules of an extension."""
    _REGISTERED_RULES[:] = [item for item in _REGISTERED_RULES if item[2] != owner]


def build_default_rule_chain() -> RuleChain:  # noqa: F811
    """Build the built-in rule chain, then insert the extension rules.

    扩展规则按注册顺序插入其锚点之后：同一锚点的多条规则，先注册的先执行，
    后注册的排在它后面（而不是排在它前面）。
    """
    chain = _build_builtin_rule_chain()
    # Group the rules by anchor. The group keeps the registration order.
    groups: dict[str, list] = {}
    for rule, after, _owner in _REGISTERED_RULES:
        groups.setdefault(after, []).append(_IsolatedRule(rule))
    for anchor, rules in groups.items():
        names = [getattr(item, "rule_name", "") for item in chain.rules]
        index = names.index(anchor) + 1 if anchor in names else len(chain.rules)
        chain.rules[index:index] = rules
    return chain
