"""Import the dormant ASGI application in the release fixture's forkserver."""

import gc
import threading

from app.main import app as app

# The forkserver handles no requests. Connections and other runtime state are
# opened by the original lifespan in each of the three child processes.
assert threading.active_count() == 1
assert not hasattr(app.state, "database")
gc.freeze()
