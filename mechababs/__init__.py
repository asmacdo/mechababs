"""mechababs — automation glue for running BIDS apps across datasets via BABS.

A campaign lives inside a study: ``mechababs campaign init`` writes its config,
pins its environment, and creates its statefile there, and the operating verbs
(``add-dataset`` / ``iterate`` / ``status`` / ``retire-derivative``) advance it
from that study. ``cli`` is the user-facing entrypoint; ``inner`` carries the
action verbs ``iterate`` dispatches under ``datalad run``.
"""

try:
    from ._version import __version__
except ImportError:
    __version__ = "0+unknown"
