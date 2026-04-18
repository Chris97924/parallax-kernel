"""Provider backends for parallax.extract.

The base Protocol lives in :mod:`parallax.extract.providers.base`. Concrete
backends (``mock``, ``openrouter``, ``claude_subprocess``) are imported on
demand — importing the ``providers`` package itself stays cheap.
"""
