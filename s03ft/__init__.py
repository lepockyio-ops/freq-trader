"""S-03 freqtrade 交易流程的统一命令行入口 `s03ft`。

设计给 OpenClaw agent 调用：每个子命令参数固定、可用 --json 输出、只做白名单内的动作。
交易决策始终由 freqtrade + S03Strategy 完成；本库只负责运维、研究、复盘与告警相关的操作。
"""
__version__ = "1.0.0"
