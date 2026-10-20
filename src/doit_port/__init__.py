"""doit-port: portable, content-addressed build receipts for `doit`.

The package plugs into three *public* extension points of `doit` and adds
nothing to its scheduler, dependency resolver or CLI core:

1. the ``uptodate`` protocol -- a documented hook that lets a task decide by
   itself whether it is up to date (``doit.dependency.Dependency.get_status``
   calls it before the built-in file/target checks);
2. the ``doit.COMMAND`` entry-point group -- the documented way for a
   third-party package to add a ``doit <name>`` sub-command;
3. the ``meta`` task attribute -- documented as "extra info from user/plugin
   not directly used by doit".

Nothing inside ``doit`` is monkey-patched or vendored.
"""

from .api import port_group, port_task, describe
from .config import PortConfig, load_config
from .errors import (
    ConfigError,
    PortError,
    ReceiptCorrupt,
    StoreCorrupt,
    UnsupportedTask,
)
from .hook import PortHook
from .store import ObjectStore

__version__ = "0.1.0"
SCHEMA_VERSION = 1

__all__ = [
    "port_task",
    "port_group",
    "setup",
    "describe",
    "PortConfig",
    "load_config",
    "PortHook",
    "ObjectStore",
    "PortError",
    "ConfigError",
    "UnsupportedTask",
    "ReceiptCorrupt",
    "StoreCorrupt",
    "__version__",
    "SCHEMA_VERSION",
]

from .api import setup  # noqa: E402  (re-export; kept after __all__ for clarity)
