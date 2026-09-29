"""AI insights & reports.

The intelligence layer observes the StatsStore + RiskMonitor and produces
human-readable reports. It can use an LLM if available, but degrades to a
pure-template report otherwise so the insights always work.
"""
