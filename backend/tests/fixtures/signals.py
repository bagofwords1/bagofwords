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
    once the wait ends. The block's exit stops and joins the senders, then
    restores the previous handler. ``signal.signal`` first runs the Python
    handlers of signals still pending, so a signal sent here that is still
    pending reaches this (by then no-op) handler, not the previous one."""
    assert threading.current_thread() is threading.main_thread()
    main = threading.main_thread().ident
    handled = threading.Event()
    fired = False  # checked first: the handler never waits on ``handled``'s lock
    senders = []
    senders_lock = threading.Lock()  # append+start as one: never join an unstarted sender

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
        with senders_lock:
            senders.append(sender)
            sender.start()

    previous = signal.signal(signum, handler)
    try:
        yield send
    finally:
        try:
            fired = True
            handled.set()
            with senders_lock:
                started = list(senders)
            for sender in started:
                sender.join()
        finally:
            signal.signal(signum, previous)


@contextlib.contextmanager
def handling_sigint():
    """Within the block SIGINT raises KeyboardInterrupt, as it does in an
    interactive run, also when the process inherited it ignored (a
    background job of a non-interactive shell: ``nohup pytest &``, an ssh
    background run) or set to the default action, under which
    ``_thread.interrupt_main()`` does nothing. Outside the main thread, or
    when SIGINT has a Python handler already, it changes nothing."""
    previous = signal.getsignal(signal.SIGINT)
    if threading.current_thread() is not threading.main_thread() or previous not in (
        signal.SIG_IGN,
        signal.SIG_DFL,
    ):
        yield
        return
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
