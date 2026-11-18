"""Signals for tests that interrupt the main thread while it is blocked
(tests/unit/test_test_client_loop.py and the pytest run it starts)."""
import contextlib
import signal
import sys
import threading
import time


@contextlib.contextmanager
def retrying_signal(signum, act):
    """Within the block, ``signum``'s handler calls ``act(signum, frame)``
    the first time it runs (later signals are no-ops), and the ``send`` it
    yields (callable from any thread) starts a thread that sends ``signum``
    to the main thread every 10 ms until that handler has run, giving up
    (and saying so) after about 5 s. One signal is not enough to interrupt a
    wait: one that lands after the main thread released the GIL but before
    it started its lock wait interrupts no wait, and its handler runs only
    once the wait ends. The block's exit blocks ``signum`` in the main
    thread, stops and joins the senders, drops what they left pending and
    restores the previous handler before it unblocks ``signum``, so no
    signal sent here reaches that handler, and none raises in the exit."""
    assert threading.current_thread() is threading.main_thread()
    main = threading.main_thread().ident
    handled = threading.Event()
    fired = False  # checked first: the handler never waits on ``handled``'s lock
    senders = []

    def handler(signum_, frame):
        nonlocal fired
        if fired:
            return
        fired = True
        handled.set()
        act(signum_, frame)

    def keep_sending():
        deadline = time.monotonic() + 5
        while not handled.is_set() and time.monotonic() < deadline:
            signal.pthread_kill(main, signum)
            handled.wait(0.01)
        if not handled.is_set():
            print(
                f"Warning: retrying_signal: signal {signum} was not handled within 5s; "
                "no longer sending it",
                file=sys.stderr,
            )

    def send():
        sender = threading.Thread(target=keep_sending, name="test-signal-sender", daemon=True)
        senders.append(sender)
        sender.start()

    previous = signal.signal(signum, handler)
    try:
        yield send
    finally:
        fired = True
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signum})
        try:
            handled.set()
            for sender in list(senders):
                sender.join()
            while signal.sigtimedwait({signum}, 0) is not None:
                pass  # sent here, still pending: dropped
            signal.signal(signum, previous)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
