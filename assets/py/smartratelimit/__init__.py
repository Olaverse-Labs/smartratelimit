"""Minimal package init for the browser playground.

Not the library's real __init__.py: that eagerly imports the HTTP and storage
stack, and sqlite3 is unvendored under Pyodide. The simulator modules alongside
this file are copied verbatim from the package and need none of it.
"""
