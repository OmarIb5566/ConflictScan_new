"""
AI fleet - several local models, each doing the job it is best at
==================================================================
    fleet.llm(role)       a client bound to one role (see config/fleet.toml)
    fleet.call(...)       one request; fleet.embed(texts); fleet.pmap(fn, items, role)
    fleet.profile         the active profile, features, per-role settings
    fleet.verify          second opinion on findings (and votes on critical ones)
    fleet.memory          searchable memory of past RFIs and client responses

Windows compatible.
"""

from fleet import profile  # noqa: F401
from fleet.client import (  # noqa: F401
    ModelUnavailable, call, cosine, current_run, embed, llm, pmap, resolve, resolved_model,
)
