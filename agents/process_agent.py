"""
ProcessAgent — represents one process station.

All Modbus signals are holding registers (no coils, no discrete inputs).

Modbus protocol (from reference):
  Write 1 to 'run'  →  isRunning goes to 1 (process active)
  isRunning goes to 0  →  process complete
  Always check 'sensor' = 1 before starting (part must be present).
"""

import time
import threading
import logging

from agents.base_agent import BaseAgent
from agents.directory_facilitator import DirectoryFacilitator
from core.message import Message, Performative

logger = logging.getLogger(__name__)

_START_TIMEOUT   = 30.0    # seconds to wait for isRunning to go 1 after writing run=1
_PROCESS_TIMEOUT = 30.0   # seconds to wait for isRunning to go back to 0
_SENSOR_TIMEOUT  = 1.0    # quick sanity check — transport already guarantees part presence


class ProcessAgent(BaseAgent):

    def __init__(self, agent_id: str, modbus, modbus_map: dict, station_config: dict):
        super().__init__(agent_id)
        self._mb           = modbus
        self._smap         = modbus_map["stations"].get(agent_id, {})
        self._capabilities = station_config.get("capabilities", ["process_op"])
        self._location     = {"x": station_config["x"], "y": station_config["y"]}
        self._failed       = False
        self._fail_lock    = threading.Lock()

    # ------------------------------------------------------------------
    # Failure API — called from main / interactive CLI for R4 demo
    # ------------------------------------------------------------------

    def trigger_failure(self):
        with self._fail_lock:
            self._failed = True
        if hasattr(self._mb, "trigger_station_failure"):
            self._mb.trigger_station_failure(self.agent_id)
        DirectoryFacilitator().set_status(self.agent_id, "failed")
        self.logger.warning("FAILURE TRIGGERED")

    def clear_failure(self):
        with self._fail_lock:
            self._failed = False
        if hasattr(self._mb, "clear_station_failure"):
            self._mb.clear_station_failure(self.agent_id)
        DirectoryFacilitator().set_status(self.agent_id, "active")
        self.logger.info("Failure cleared, back online")

    def is_failed(self) -> bool:
        with self._fail_lock:
            return self._failed

    # ------------------------------------------------------------------
    # Processing via holding registers
    # ------------------------------------------------------------------

    def _do_process(self, part_id: str) -> bool:
        run_reg        = self._smap.get("run")
        is_running_reg = self._smap.get("is_running")
        sensor_reg     = self._smap.get("sensor")

        if run_reg is None or is_running_reg is None:
            self.logger.error("No run/is_running registers configured for %s", self.agent_id)
            return False

        if self.is_failed():
            return False

        # 1. Wait for sensor to confirm part is present
        if sensor_reg is not None:
            deadline = time.time() + _SENSOR_TIMEOUT
            while time.time() < deadline:
                if self.is_failed():
                    return False
                if self._mb.read_holding_register(sensor_reg) == 1:
                    break
                time.sleep(0.1)
            else:
                self.logger.warning("%s: part sensor not ready before timeout", self.agent_id)

        if self.is_failed():
            return False

        # 2. Start the process
        self._mb.write_holding_register(run_reg, 1)
        self.logger.info("Run the process" , run_reg , 1)

        # 3. Wait for isRunning = 1 (process has started)
        deadline = time.time() + _START_TIMEOUT
        started = False
        while time.time() < deadline:
            if self.is_failed():
                return False
            if self._mb.read_holding_register(is_running_reg) == 1:
                started = True
                break
            time.sleep(0.05)

        if not started:
            self.logger.error("%s: process never started (isRunning stayed 0)", self.agent_id)
            return False

        # 4. Wait for isRunning = 0 (process done)
        deadline = time.time() + _PROCESS_TIMEOUT
        while time.time() < deadline:
            if self.is_failed():
                return False
            if self._mb.read_holding_register(is_running_reg) == 0:
                self.logger.info("%s: processing complete", self.agent_id)
                return True
            time.sleep(0.1)

        self.logger.error("%s: process timeout", self.agent_id)
        return False

    # ------------------------------------------------------------------
    # Agent loop
    # ------------------------------------------------------------------

    def _run(self):
        df = DirectoryFacilitator()
        df.register(
            self.agent_id,
            self._capabilities,
            location=self._location,
        )
        self.logger.info("Ready  caps=%s  loc=%s", self._capabilities, self._location)

        while self._running:
            msg = self.receive(timeout=1.0)
            if msg is None:
                continue
            if (msg.performative == Performative.REQUEST
                    and msg.content.get("action") == "process"):
                self.logger.info(f"Request granted to process" , msg.sender , 
                                 msg.receiver , msg.reply_to)
                self._handle_process(msg)

    def _handle_process(self, msg: Message):
        part_id = msg.content.get("part_id", "?")

        if self.is_failed():
            self.logger.warning("Rejecting request — station failed")
            self.send(msg.create_reply(
                Performative.FAILURE, self.agent_id,
                {"part_id": part_id, "reason": "station_failed", "action": "process_failed"},
            ))
            return

        self.send(msg.create_reply(
            Performative.AGREE, self.agent_id,
            {"part_id": part_id, "status": "processing"},
        ))

        ok = self._do_process(part_id)

        self.send(msg.create_reply(
            Performative.INFORM if ok else Performative.FAILURE,
            self.agent_id,
            {"action": "process_done" if ok else "process_failed",
             "part_id": part_id,
             **({"reason": "station_failed"} if not ok else {})},
        ))
