"""Bounded ownership for opt-in pipe I/O. No native calls or startup on import.

Unconfirmed operations stay strongly rooted, including their connection.
No destructor, background reaper, automatic restart or permission is provided.
Native calls must hold the operation gate; reaping never waits for that gate.
HOLD_LIVE: mock coverage does not establish Windows cancellation acceptance.
"""
from threading import Lock, RLock


class PipeLifetimeError(OSError):
    pass


class _Operation:
    def __init__(self, connection, size):
        self.connection, self.size = connection, size
        self.gate = Lock()
        self.gate.acquire()
        self.buffer = self.overlapped = self.event = None
        self.close_event = self.probe = None
        self.terminal = True  # Nothing has been issued yet.
        self.expired = self.cancel_requested = False
        self.close_attempted = False


class PipeConnection:
    def __init__(self, registry):
        self.registry = registry
        self.handle = None
        self.disconnect = self.close_handle = None
        self.closing = self.close_attempted = False

    def bind(self, handle, *, disconnect, close):
        if type(handle) is not int or handle <= 0 or not callable(disconnect) or not callable(close):
            raise ValueError("valid owned pipe handle and cleanup required")
        with self.registry._lock:
            if self.handle is not None or self.closing or self not in self.registry._connections:
                raise PipeLifetimeError("connection cannot be rebound")
            if any(c.handle == handle for c in self.registry._connections if c is not self):
                raise PipeLifetimeError("pipe handle already owned")
            self.handle, self.disconnect, self.close_handle = handle, disconnect, close

    def close(self):
        with self.registry._lock:
            self.closing = True
        self.registry._close_connection(self)


class PipeIoRegistry:
    def __init__(self, *, max_connections=4, max_operations=8, max_bytes=128 * 1024):
        for value, upper in ((max_connections, 4), (max_operations, 8), (max_bytes, 128 * 1024)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("invalid pipe resource budget")
        self._limits = max_connections, max_operations, max_bytes
        self._lock = RLock()
        self._connections, self._operations = set(), set()
        self._bytes = 0
        self._halted = False

    def snapshot(self):
        with self._lock:
            return {"connections": len(self._connections), "operations": len(self._operations),
                    "buffer_bytes": self._bytes, "halted": self._halted}

    def reserve_connection(self):
        with self._lock:
            if self._halted or len(self._connections) >= self._limits[0]:
                raise PipeLifetimeError("pipe admission stopped or full")
            connection = PipeConnection(self)
            self._connections.add(connection)
            return connection

    def begin(self, handle, size):
        if type(size) is not int or not 0 <= size <= 8196:
            raise ValueError("invalid operation buffer size")
        with self._lock:
            connection = next((c for c in self._connections if c.handle == handle), None)
            if (self._halted or connection is None or connection.closing
                    or any(op.connection is connection for op in self._operations)
                    or len(self._operations) >= self._limits[1]
                    or self._bytes + size > self._limits[2]):
                raise PipeLifetimeError("owned idle pipe and resource budget required")
            operation = _Operation(connection, size)
            self._operations.add(operation)
            self._bytes += size
            return operation

    def finish(self, operation):
        """Called with operation gate held after ALL native calls have returned."""
        try:
            if operation.terminal:
                self._retire(operation)
            else:
                with self._lock:
                    self._halted = True
        finally:
            operation.gate.release()

    def _retire(self, operation):
        if operation.event is not None:
            if operation.close_attempted:
                return  # Unknown close result: never retry a possibly reused handle.
            operation.close_attempted = True
            try:
                if not operation.close_event(operation.event):
                    raise PipeLifetimeError("event close unconfirmed")
            except BaseException:
                with self._lock:
                    self._halted = True
                raise
        with self._lock:
            self._operations.remove(operation)
            self._bytes -= operation.size
        self._close_connection(operation.connection)

    def _close_connection(self, connection):
        with self._lock:
            if (connection not in self._connections or not connection.closing
                    or connection.close_attempted
                    or any(op.connection is connection for op in self._operations)):
                return
            connection.close_attempted = True
        try:
            if connection.handle is not None:
                # A nonconnected pipe can report FALSE on disconnect; completion
                # has already been established for every operation before here.
                connection.disconnect(connection.handle)
                if not connection.close_handle(connection.handle):
                    raise PipeLifetimeError("pipe close unconfirmed")
        except BaseException:
            with self._lock:
                self._halted = True
            raise
        with self._lock:
            self._connections.remove(connection)

    def reap_once(self):
        """One bounded sweep, no wait(TRUE), retry loop or admission reset.

        May be called by an independently supervised recovery/diagnostic owner.
        This does not guarantee that a broken native API itself returns on time.
        """
        with self._lock:
            operations = tuple(self._operations)
        for operation in operations:
            if not operation.gate.acquire(blocking=False):
                continue
            try:
                with self._lock:
                    if operation not in self._operations:
                        continue
                if not operation.terminal and operation.probe is not None:
                    operation.probe()
                if operation.terminal:
                    self._retire(operation)
            finally:
                operation.gate.release()
        return self.snapshot()


REGISTRY = PipeIoRegistry()
