"""Bounded latest-frame mailbox for an existing camerad DRIVER stream.

VisionIPC connect(False) can still wait for the FD handshake. Keep all IPC on
one persistent worker, never on the GL thread. This opens no camera device.
"""
import threading
import time


class DriverCameraFeed:
    def __init__(self, client_factory):
        self._factory = client_factory
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._active = False
        self._generation = 0
        self._latest = None
        self._thread = threading.Thread(target=self._run, name='hud-driver-ipc', daemon=True)
        self._thread.start()

    def set_active(self, active):
        with self._lock:
            if active != self._active:
                self._generation += 1
                self._active = active
                self._latest = None
        self._wake.set()

    def snapshot(self):
        with self._lock:
            latest = self._latest
        if latest is None or time.monotonic() - latest[2] > 1.2:
            return None
        return latest

    def close(self):
        self.set_active(False)
        self._stop.set()
        self._wake.set()

    def _run(self):
        client = None
        generation = -1
        last_frame = 0.0
        while not self._stop.is_set():
            with self._lock:
                active, current_generation = self._active, self._generation
            if current_generation != generation:
                client = None
                generation = current_generation
            if not active:
                client = None
                self._wake.wait(.1)
                self._wake.clear()
                continue
            try:
                if client is None:
                    client = self._factory()
                if not client.is_connected():
                    if not client.connect(False) or not client.num_buffers:
                        client = None
                        self._stop.wait(.5)
                        continue
                    last_frame = time.monotonic()
                frame = client.recv(timeout_ms=0)
                now = time.monotonic()
                if frame is not None:
                    last_frame = now
                    with self._lock:
                        if self._active and generation == self._generation and not self._stop.is_set():
                            # Retain the owner while GL may still reference its buffers.
                            self._latest = (client, frame, now)
                elif not client.is_connected() or now - last_frame > 1.2:
                    with self._lock:
                        self._latest = None
                    client = None
                self._stop.wait(.03)
            except Exception:
                with self._lock:
                    self._latest = None
                client = None
                self._stop.wait(.5)
        with self._lock:
            self._latest = None
