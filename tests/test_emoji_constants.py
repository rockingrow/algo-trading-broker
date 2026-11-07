"""
The glyph table is a contract kept by convention: ``quant-trading-engine``
carries its own copy in ``src/qte_shared/emoji_constants.py``, and the bot
re-renders the broker's trade card from ``bot/app/emojis.py``. One trade is
reported by all three, so these tests pin the entry icons rather than leaving
them to agree by accident.
"""

from broker.helpers import emoji_constants as em

GREEN_SQUARE = "\U0001f7e9"
RED_SQUARE = "\U0001f7e5"


def test_an_entry_is_drawn_as_a_square():
  assert em.LONG == GREEN_SQUARE
  assert em.SHORT == RED_SQUARE


def test_an_entry_is_not_drawn_as_a_flag_a_status_or_a_stop():
  """
  A circle carries too many other meanings in this table to also mean "a
  position was opened" — reusing one is what made a green dot ambiguous.
  """
  circles = {
    em.FLAG_ON,
    em.FLAG_OFF,
    em.BROKER_STARTED,
    em.NATS_DISCONNECTED,
    em.SL,
    em.TRADE_OPENED,
  }
  assert em.LONG not in circles
  assert em.SHORT not in circles


def test_the_two_entry_icons_are_told_apart():
  assert em.LONG != em.SHORT
