"""Exceptions raised at PineForge integration boundaries."""


class EngineStreamError(RuntimeError):
    """A PineForge streaming C ABI call failed."""
