from monitors import Monitors

def test_stop_then_is_stopped():
    m = Monitors()
    m.start("d"); assert m.is_stopped("d") is False
    m.stop("d");  assert m.is_stopped("d") is True

def test_start_clears_stop():
    m = Monitors()
    m.stop("d"); m.start("d")
    assert m.is_stopped("d") is False

def test_stop_all_halts_every_active_monitor():
    # a new deploy must stop every prior deploy's monitor (single-deploy design)
    m = Monitors()
    m.start("demo"); m.start("apex")
    m.stop_all()
    assert m.is_stopped("demo") is True and m.is_stopped("apex") is True
    # a fresh start re-activates only that one
    m.start("apex")
    assert m.is_stopped("apex") is False and m.is_stopped("demo") is True
