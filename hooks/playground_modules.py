"""
MkDocs hook: publish the simulator's source for the browser playground.

The playground runs smartratelimit inside the reader's browser under Pyodide.
Installing it from PyPI does not work, for two independent reasons:

1. ``smartratelimit/__init__.py`` eagerly imports the HTTP and storage stack,
   and ``storage.py`` imports ``sqlite3`` — which Pyodide unvendors from the
   standard library. Importing the package therefore fails outright.
2. PyPI serves whatever was last released, which is not necessarily the code
   these docs describe. A page that demonstrates the docs should run the
   source the docs were built from.

So instead of installing anything, this hook copies the modules the simulator
actually needs into the built site. The browser writes them straight into
Pyodide's filesystem and imports them. They pull in nothing but the standard
library, so no dependency has to resolve at all.

The files are copied verbatim: the playground runs the same source as the
package, and ``tests/test_playground.py`` fails if the two ever diverge.

It lives outside ``docs/`` on purpose — anything under the docs directory is
copied into the built site, and this file has no business being published.
"""

import shutil
from pathlib import Path

#: Where the browser looks for them, relative to the site's assets root.
DESTINATION = "py/smartratelimit"

#: Everything ``smartratelimit.simulate`` needs, and nothing more. Each of these
#: imports only the standard library plus the others, which is what makes the
#: whole approach work under Pyodide.
MODULES = ("_time.py", "models.py", "simulate.py")

#: A package needs an ``__init__.py``, but not the real one: that is what drags
#: in requests and sqlite3. The simulator's modules do not use the re-exports it
#: provides, so a stub is faithful for this purpose — and says so, in case
#: someone finds it in the deployed site and wonders.
STUB_INIT = '''"""Minimal package init for the browser playground.

Not the library's real __init__.py: that eagerly imports the HTTP and storage
stack, and sqlite3 is unvendored under Pyodide. The simulator modules alongside
this file are copied verbatim from the package and need none of it.
"""
'''

_PACKAGE = Path(__file__).resolve().parent.parent / "smartratelimit"


def on_post_build(config, **kwargs):
    """Copy the simulator modules into the built site."""
    destination = Path(config["site_dir"]) / "assets" / DESTINATION
    destination.mkdir(parents=True, exist_ok=True)

    (destination / "__init__.py").write_text(STUB_INIT)
    for name in MODULES:
        shutil.copyfile(_PACKAGE / name, destination / name)
