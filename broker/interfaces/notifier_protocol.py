from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover — import only for type checkers
  from broker.schemas.webhook_schema import WebhookPayload


@runtime_checkable
class Notifier(Protocol):
  """Anything that can deliver a human-readable message to an external channel."""

  async def send_message(self, message_text: str) -> None: ...


@runtime_checkable
class SignalBroadcaster(Protocol):
  """Publishes a signal into the broadcast channels as part of its *cycle*.

  Distinct from :class:`Notifier` because a broadcast is not fire-and-forget
  text: the first signal of a cycle creates a message and every later one
  rewrites it, so the implementation owns state (which message id each chat
  holds) rather than just a destination.
  """

  async def broadcast(
    self, payload: "WebhookPayload", *, attempt_number: int | None = None
  ) -> None: ...
