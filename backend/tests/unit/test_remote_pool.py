import pytest

from app.data_sources.clients.remote_pool import RemotePool


class Transport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_reuse_isolated_by_scope_and_rotated_by_settings():
    pool = RemotePool()
    try:
        with pool.lease("org:user:connection", "config1", Transport) as first:
            pass
        with pool.lease("org:user:connection", "config1", Transport) as again:
            assert again is first
        with pool.lease("org:other:connection", "config1", Transport) as other:
            assert other is not first
        with pool.lease("org:user:connection", "config2", Transport) as rotated:
            assert rotated is not first
            assert first.closed
    finally:
        pool.close()


def test_idle_expiry_and_capacity_release_transports():
    now = [0]
    pool = RemotePool(capacity=1, clock=lambda: now[0])
    try:
        with pool.lease("a", "v", Transport, idle=10) as first:
            pass
        now[0] = 11
        pool.expire()
        assert first.closed
        with pool.lease("b", "v", Transport) as second:
            pass
        with pool.lease("c", "v", Transport):
            assert second.closed
    finally:
        pool.close()


def test_failed_operation_is_not_replayed_and_transport_is_discarded():
    pool = RemotePool()
    calls = []
    try:
        with pytest.raises(ConnectionError), pool.lease("a", "v", Transport) as failed:
            calls.append("sent")
            raise ConnectionError("disconnected after send")
        assert calls == ["sent"]
        assert failed.closed
        with pool.lease("a", "v", Transport) as replacement:
            assert replacement is not failed
    finally:
        pool.close()
