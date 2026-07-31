"""Scoring Profile YAML resources (design §5, §6).

This package exists so the ``*.yaml`` files under ``profiles/`` ship inside the
built wheel and are locatable at runtime through ``importlib.resources``
(AIS-004 R2). The profile files remain the source of truth; this module adds no
semantics and declares no public API.
"""
