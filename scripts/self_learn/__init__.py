"""Self-learning publisher modules.

Subsystems:

- stats.py         — record every publish attempt into a queryable store
- optimizer.py     — pick the best strategy for a given (platform, content)
- risk.py          — detect ban-risk signals and recommend back-off

All three share the same on-disk event log so they see the same data.
"""
