"""Deprecated alias for `mesh.registry_app` (renamed in #33).

`mesh.service` (the FastAPI registry app) sat one letter away from `mesh.serve`
(the node serving daemon) for an entirely unrelated role — a recurring source
of confusion. It was renamed to `mesh.registry_app`, which pairs with
`mesh.registry` (the event-sourced store the app wraps).

This shim re-exports everything and emits a DeprecationWarning on import, so
existing callers keep working for one release:

    uvicorn mesh.service:app          # still serves (use mesh.registry_app:app)
    from mesh.service import create_mesh_app   # still imports

Import from `mesh.registry_app` instead.
"""

from __future__ import annotations

import warnings

from mesh.registry_app import *  # noqa: F401,F403 — back-compat re-export
from mesh.registry_app import (  # noqa: F401 — explicit: `uvicorn mesh.service:app` + common imports
    __all__,
    app,
    create_mesh_app,
)

warnings.warn(
    "mesh.service is deprecated and will be removed; import from "
    "mesh.registry_app instead (renamed in #33 to disambiguate from mesh.serve).",
    DeprecationWarning,
    stacklevel=2,
)
