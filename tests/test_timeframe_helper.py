from broker.helpers.timeframe_helper import format_timeframe


def test_minutes_below_an_hour():
  assert format_timeframe(5) == "M5"
  assert format_timeframe(45) == "M45"


def test_string_minutes_are_parsed():
  assert format_timeframe("60") == "H1"
  assert format_timeframe("120") == "H2"


def test_hours():
  assert format_timeframe(60) == "H1"
  assert format_timeframe(180) == "H3"


def test_days():
  assert format_timeframe(1440) == "D1"
  assert format_timeframe(2880) == "D2"


def test_days_take_precedence_over_hours():
  # 1440 is divisible by both 60 and 1440; days must win.
  assert format_timeframe(1440) == "D1"


def test_non_numeric_string_passthrough():
  # Already-formatted strings are returned untouched.
  assert format_timeframe("M5") == "M5"
  assert format_timeframe("H1") == "H1"


def test_zero_and_negative():
  assert format_timeframe(0) == "M0"
  assert format_timeframe(-15) == "M-15"


def test_none_passthrough():
  assert format_timeframe(None) == "None"
