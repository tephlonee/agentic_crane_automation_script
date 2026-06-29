"""
SinkAgent — terminal station that receives completed parts.
"""

import logging
import threading

from agents.base_agent import BaseAgent
from agents.directory_facilitator import DirectoryFacilitator
from core.message import Message, Performative

logger = logging.getLogger(__name__)


class SinkAgent(BaseAgent):

    def __init__(self, agent_id: str, station_config: dict):
        super().__init__(agent_id)
        self._location = {"x": station_config["x"], "y": station_config["y"]}
        self._received: list = []
        self._lock = threading.Lock()

    def get_count(self) -> int:
        with self._lock:
            return len(self._received)

    def get_received(self) -> list:
        with self._lock:
            return list(self._received)

    def _run(self):
        df = DirectoryFacilitator()
        df.register(self.agent_id, ["sink"], location=self._location)
        self.logger.info("Sink ready at %s", self._location)

        while self._running:
            msg = self.receive(timeout=1.0)
            if msg is None:
                continue

            if msg.performative == Performative.INFORM and msg.content.get("action") == "deliver":
                self._handle_delivery(msg)

    def _handle_delivery(self, msg: Message):
        part_id   = msg.content.get("part_id", "?")
        part_type = msg.content.get("part_type", "?")
        with self._lock:
            self._received.append({"part_id": part_id, "part_type": part_type})
        count = len(self._received)
        self.logger.info("Received part %s (type %s) — total: %d", part_id, part_type, count)
        print(f"  [SINK] Part {part_id} (type {part_type}) delivered — total completed: {count}")

        self.send(msg.create_reply(
            Performative.AGREE, self.agent_id,
            {"part_id": part_id, "status": "accepted", "total": count}
        ))
