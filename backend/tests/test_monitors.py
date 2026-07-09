from monitors import Monitors

def test_stop_then_is_stopped():
    m = Monitors()
    m.start("d"); assert m.is_stopped("d") is False
    m.stop("d");  assert m.is_stopped("d") is True

def test_start_clears_stop():
    m = Monitors()
    m.stop("d"); m.start("d")
    assert m.is_stopped("d") is False
