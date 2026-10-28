"""Fair, non-conflating reads from owned sockets, independent of poll wrappers."""


def receive_batch(sockets, per_service=256):
  # msgq Poller.poll() constructs new SubSocket wrappers around C++ pointers.
  # Their Python ids cannot identify the sockets originally registered.
  # Poll is only a wakeup; read the owned sockets in bounded round-robin turns.
  for service, sock in sockets.items():
    for _ in range(per_service):
      raw = sock.receive(non_blocking=True)
      if raw is None:
        break
      yield service, raw
