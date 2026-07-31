"""JSON Schema contract resources (AIS-002 §17).

This package exists so the ``*.schema.json`` files under ``schemas/`` ship inside the built
wheel and are locatable at runtime through ``importlib.resources``. The schema files remain the
source of truth; this module adds no semantics and declares no public API.
"""
