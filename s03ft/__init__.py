"""S03 freqtrade 交易流程的统一命令行入口 `s03ft`。

设计给 OpenClaw agent 调用：每个子命令参数固定、可用 --json 输出、只做白名单内的动作。
交易决策始终由 freqtrade 里你配置的策略完成（默认是空白模板 MyStrategy）；本库只负责运维、研究、复盘与告警相关的操作。
"""
__version__ = "2.1.0"
