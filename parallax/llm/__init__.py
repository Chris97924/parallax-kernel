"""LLM call surface for Parallax (ADR-006).

All code that talks to an external LLM goes through
:func:`parallax.llm.call.call`. No module outside this subpackage should
import ``google.genai`` or issue raw HTTP to model providers.

Callers must use the explicit form ``from parallax.llm.call import call``
so the submodule name ``parallax.llm.call`` is not shadowed by a re-export.
"""
