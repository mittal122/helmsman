from breakers import Breaker


def test_trips_after_max():
    b = Breaker(max_attempts=2)
    assert b.tripped("d") is False
    b.record("d"); assert b.tripped("d") is False
    b.record("d"); assert b.tripped("d") is True


def test_reset_clears():
    b = Breaker(max_attempts=1)
    b.record("d"); assert b.tripped("d") is True
    b.reset("d"); assert b.tripped("d") is False


def test_keys_independent():
    b = Breaker(max_attempts=1)
    b.record("a"); assert b.tripped("a") is True and b.tripped("b") is False
