"""
agents/crane_agent.py — controls the physical crane over Modbus TCP.

ROLE IN THE SYSTEM:
  The CraneAgent is the only agent that writes to the Modbus hardware.
  It receives a complete ROUTE from PartAgent and executes every step in
  strict sequence — the crane never interrupts itself to serve a different part.
  New routes from other PartAgents accumulate in the mailbox and are handled
  one by one in FIFO order (first-come, first-served).

MOVEMENT RULES — axes move one at a time, never diagonally:
  Moving set_x and set_y simultaneously causes the crane to travel diagonally,
  which crashes it into the station pots at pick/place height.
  The correct sequence for every transport leg is:
    1. Rise to travel_y (safe height above all obstacles)
    2. Slide horizontally to the target column (no obstacle risk at travel_y)
    3. Descend to pick_y to pick or place

SENSOR-FIRST PICKUP:
  Before the crane descends at a source station, it parks at travel_y and polls
  the source sensor register until it reads 1 (part is physically present).
  This prevents the crane from descending into an empty station.

CNP WITH PROCESSAGENT:
  For each "process" step in the route, the crane sends a REQUEST to ProcessAgent
  and waits for AGREE (acknowledged) then INFORM (processing complete).
  This keeps ProcessAgent in the communication loop — it is the one that actually
  writes run=1 to the Modbus register and monitors is_running.
  New route requests that arrive during this wait are automatically buffered by
  receive_from() and will be handled after the current route completes.

REGISTER MAP (from config/modbus_map.json):
  set_x   (reg 1)  — write target X position
  set_y   (reg 2)  — write target Y position
  vacuum  (reg 3)  — write 1 to grab, 0 to release
  pos_x   (reg 15) — read actual X position (updated by simulation)
  pos_y   (reg 16) — read actual Y position
  travel_y = 150   — safe travel height
  pick_y   = 82    — height for picking / placing parts
"""

import time
import logging

from agents.base_agent import BaseAgent
from agents.directory_facilitator import DirectoryFacilitator
from core.message import Message, Performative

logger = logging.getLogger(__name__)

# All set to 86400 (24 hours) = effectively no timeout during testing
_AGREE_TIMEOUT       = 86400.0   # wait for ProcessAgent AGREE
_PROCESS_TIMEOUT     = 86400.0   # wait for ProcessAgent INFORM (process done)
_SENSOR_WAIT_TIMEOUT = 86400.0   # wait for source sensor to go HIGH


class CraneAgent(BaseAgent):

    def __init__(self, agent_id: str, modbus, modbus_map: dict, station_cfg: dict):
        """
        agent_id    — "Crane"
        modbus      — the Modbus interface object (real or mock)
        modbus_map  — register address config loaded from config/modbus_map.json
        station_cfg — full station config (not heavily used by crane, kept for extensibility)
        """
        super().__init__(agent_id)

        self._mb       = modbus                          # Modbus read/write interface
        self._cmap     = modbus_map["crane"]             # sub-dict with crane register addresses
        self._travel_y = self._cmap["travel_y"]         # safe travel height (150)
        self._pick_y   = self._cmap["pick_y"]           # pick/place height (82)

        # Build a lookup table: station_name → sensor register address
        # Used by _wait_for_sensor() to know which register to poll before picking.
        # Only stations that have a "sensor" entry in the map are included.
        self._station_sensors = {
            name: cfg["sensor"]
            for name, cfg in modbus_map.get("stations", {}).items()
            if "sensor" in cfg
        }
        # Result example: {"Source1":17, "Source2":18, "Process1":21, "Process2":22}

    # ------------------------------------------------------------------
    # Single-axis movement — never move both axes at once
    # ------------------------------------------------------------------

    def _wait_axis(self, pos_reg: int, target: int) -> bool:
        """
        Poll the position register until the crane reaches the target (within tolerance).

        pos_reg — the register to read (pos_x or pos_y)
        target  — the value we are waiting for
        tol     — how close is "close enough" (position_tolerance, default 2 units)

        The simulation updates pos_x/pos_y gradually as the crane moves.
        We poll every 50ms and return True when the crane arrives.
        Returns False only if the move_timeout expires (set to 86400s = never during testing).
        """
        tol      = self._cmap.get("position_tolerance", 2)
        timeout  = self._cmap.get("move_timeout", 86400.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if abs(self._mb.read_holding_register(pos_reg) - target) <= tol:
                return True      # arrived — within tolerance
            time.sleep(0.05)     # 50ms poll interval
        self.logger.error("Axis timeout  reg=%d  target=%d", pos_reg, target)
        return False

    def _move_x(self, x: int) -> bool:
        """Write set_x and wait until pos_x reaches x. Horizontal movement only."""
        self.logger.debug("X → %d", x)
        self._mb.write_holding_register(self._cmap["set_x"], x)   # command the crane
        return self._wait_axis(self._cmap["pos_x"], x)             # wait for arrival

    def _move_y(self, y: int) -> bool:
        """Write set_y and wait until pos_y reaches y. Vertical movement only."""
        self.logger.debug("Y → %d", y)
        self._mb.write_holding_register(self._cmap["set_y"], y)
        return self._wait_axis(self._cmap["pos_y"], y)

    def _set_vacuum(self, engage: bool):
        """
        Engage (1) or release (0) the vacuum gripper.

        After writing, we sleep for gripper_delay (0.3s by default) to let the
        vacuum build up or release before the crane starts moving again.
        If we move too quickly after grabbing, the part may not be held securely.
        """
        self._mb.write_holding_register(self._cmap["vacuum"], 1 if engage else 0)
        time.sleep(self._cmap.get("gripper_delay", 0.3))   # wait for vacuum to stabilise

    # ------------------------------------------------------------------
    # Sensor-first pickup — park at travel_y and wait for part
    # ------------------------------------------------------------------

    def _wait_for_sensor(self, station_name: str):
        """
        Before descending at a source station, confirm a part is physically there.

        The crane has already moved to the station's X column at travel_y.
        It now polls the sensor register until it reads 1 (part present) or
        times out.  On timeout, it logs a warning and continues anyway (a later
        vacuum failure will indicate no part was actually grabbed).

        For process stations (Process1, Process2), the sensor is always 1 after
        the crane placed a part there, so this returns immediately.
        For Sink, there is no sensor register, so it also returns immediately.
        """
        sensor_reg = self._station_sensors.get(station_name)
        if sensor_reg is None:
            return   # no sensor for this station — proceed immediately (e.g. Sink)

        if self._mb.read_holding_register(sensor_reg) == 1:
            return   # part already present — no need to wait

        self.logger.info("Waiting for part at %s (reg %d)…", station_name, sensor_reg)
        deadline = time.time() + _SENSOR_WAIT_TIMEOUT
        while time.time() < deadline:
            if self._mb.read_holding_register(sensor_reg) == 1:
                self.logger.info("Part detected at %s", station_name)
                return
            time.sleep(0.1)   # poll every 100ms — gentle on CPU

        self.logger.warning("Sensor timeout at %s — proceeding anyway", station_name)

    # ------------------------------------------------------------------
    # CNP with ProcessAgent — called inside route execution
    # ------------------------------------------------------------------

    def _request_process(self, station: str, part_id: str) -> bool:
        """
        Send a REQUEST to ProcessAgent, wait for AGREE, then wait for INFORM.

        This is a full CNP exchange from inside the route execution loop.
        The crane BLOCKS here until the process station finishes — this is
        intentional and is the mechanism that ensures the crane does not pick
        up another part while the process is running.

        Meanwhile, any new execute_route messages from other PartAgents arrive
        in the crane's mailbox.  receive_from() temporarily buffers them
        (they are NOT lost) and they will be processed after this route finishes.

        Returns True if ProcessAgent sent INFORM (success), False on failure.
        """
        self.logger.info("Requesting process at %s for %s", station, part_id)

        # Send REQUEST to ProcessAgent
        self.send(Message(
            performative=Performative.REQUEST,
            sender=self.agent_id,
            receiver=station,   # "Process1" or "Process2"
            content={"action": "process", "part_id": part_id},
        ))

        # Wait for AGREE — ProcessAgent confirms it received and accepted the request
        agree = self.receive_from(station, timeout=_AGREE_TIMEOUT)
        if agree is None:
            self.logger.error("No AGREE from %s", station)
            return False
        if agree.performative == Performative.FAILURE:
            # ProcessAgent immediately refused — station was already failed
            self.logger.warning("%s refused (station failed)", station)
            return False

        # Wait for INFORM — ProcessAgent tells us the process is done
        # (This can take several seconds — the process station is running)
        result = self.receive_from(station, timeout=_PROCESS_TIMEOUT)
        if result is None:
            self.logger.error("Process timeout at %s", station)
            return False

        if result.performative == Performative.INFORM:
            self.logger.info("Process at %s done", station)
            return True

        # ProcessAgent sent FAILURE — something went wrong during processing
        self.logger.warning("Process FAILED at %s", station)
        return False

    # ------------------------------------------------------------------
    # Route execution — the heart of CraneAgent
    # ------------------------------------------------------------------

    def _execute_route(self, route: list, part_id: str):
        """
        Execute every step in the route in strict sequential order.

        Returns: (success: bool, failed_at: str|None, part_at: str|None)
          success   — True if all steps completed
          failed_at — which station caused a failure (or None on success)
          part_at   — where the part physically is when we return (or None)

        Step types:
          "pick"    — rise to travel_y, slide to station X, wait for sensor,
                      descend to pick_y, grab with vacuum, rise to travel_y
          "place"   — slide to destination X, descend to pick_y, release vacuum,
                      rise to travel_y
          "process" — send REQUEST to ProcessAgent, wait for AGREE + INFORM
        """
        part_at = None   # tracks where the part currently is (updated after each "place")

        for step in route:
            action  = step["action"]
            station = step.get("station", "")

            # ──────── PICK STEP ────────
            if action == "pick":
                x = step["x"]   # X coordinate of the station to pick from

                # 1. Rise to safe travel height (vertical move only)
                if not self._move_y(self._travel_y):
                    return False, None, part_at

                # 2. Slide horizontally to the source column (horizontal move only)
                if not self._move_x(x):
                    return False, None, part_at

                # 3. Wait here until the sensor confirms a part is present
                #    (crane is parked at travel_y above the station — safe position)
                self._wait_for_sensor(station)

                # 4. Descend to pick height
                if not self._move_y(self._pick_y):
                    return False, None, part_at

                # 5. Engage vacuum (grip the part)
                self._set_vacuum(True)

                # 6. Rise back to travel height (now carrying the part)
                if not self._move_y(self._travel_y):
                    self._set_vacuum(False)   # release if rise fails — don't carry blindly
                    return False, None, part_at

                self.logger.info("%s: picked from %s", part_id, station)

            # ──────── PLACE STEP ────────
            elif action == "place":
                x = step["x"]   # X coordinate of the destination station

                # 1. Slide to the destination column (still at travel_y, holding part)
                if not self._move_x(x):
                    self._set_vacuum(False)
                    return False, None, part_at

                # 2. Descend to place height
                if not self._move_y(self._pick_y):
                    self._set_vacuum(False)
                    return False, None, part_at

                # 3. Release vacuum (drop the part)
                self._set_vacuum(False)

                # 4. Rise to travel height (crane is now free, part is at the station)
                if not self._move_y(self._travel_y):
                    return False, None, part_at

                part_at = station   # record where the part now is
                self.logger.info("%s: placed at %s", part_id, station)

            # ──────── PROCESS STEP ────────
            elif action == "process":
                # Ask ProcessAgent to run the process; wait until it finishes.
                # Crane stays at current position (travel_y above the process station)
                # while ProcessAgent controls the process and monitors is_running.
                ok = self._request_process(station, part_id)
                if not ok:
                    # Process failed — report back which station failed and where the part is
                    return False, station, part_at

        # All steps completed successfully
        return True, None, part_at

    # ------------------------------------------------------------------
    # Agent loop — the crane's main thread
    # ------------------------------------------------------------------

    def _run(self):
        """
        CraneAgent's main loop.

        Registers in the DF, then loops forever checking for execute_route messages.
        Each route is handled to completion before the next one is read.
        This is the key serialisation mechanism — no two routes run concurrently.
        """
        df = DirectoryFacilitator()
        df.register(self.agent_id, ["crane"], metadata={"type": "crane"})
        self.logger.info(
            "Ready  travel_y=%d  pick_y=%d  sensors=%s",
            self._travel_y, self._pick_y, list(self._station_sensors),
        )

        while self._running:
            # Block for up to 1 second waiting for the next message.
            # Returns immediately if a message is already queued.
            msg = self.receive(timeout=1.0)
            if msg is None:
                continue   # no message — loop and check _running again

            if (msg.performative == Performative.REQUEST
                    and msg.content.get("action") == "execute_route"):
                self._handle_route(msg)   # this blocks until the full route is done

    def _handle_route(self, msg: Message):
        """
        Handle one execute_route request from a PartAgent.

        Sequence:
          1. Send AGREE immediately so PartAgent knows we received the route.
          2. Execute the entire route (blocks until complete).
          3. Send INFORM (success) or FAILURE with location information.

        The crane does NOT return to its receive() loop until step 3 is done.
        This is what serialises routes — only one runs at a time.
        """
        route   = msg.content["route"]
        part_id = msg.content.get("part_id", "?")

        self.logger.info("Starting route for %s (%d steps)", part_id, len(route))

        # Step 1: Acknowledge receipt — PartAgent is waiting for this
        self.send(msg.create_reply(
            Performative.AGREE, self.agent_id,
            {"part_id": part_id, "steps": len(route)},
        ))

        # Step 2: Execute — this is where all the Modbus writes happen
        ok, failed_at, part_at = self._execute_route(route, part_id)

        # Step 3: Report result to PartAgent
        if ok:
            self.logger.info("Route for %s complete", part_id)
            self.send(msg.create_reply(
                Performative.INFORM, self.agent_id,
                {"action": "route_done", "part_id": part_id},
            ))
        else:
            self.logger.error(
                "Route for %s FAILED  failed_at=%s  part_at=%s",
                part_id, failed_at, part_at,
            )
            self.send(msg.create_reply(
                Performative.FAILURE, self.agent_id,
                {
                    "action":    "route_failed",
                    "part_id":   part_id,
                    "failed_at": failed_at,   # which station caused the failure
                    "part_at":   part_at,     # where the part physically is now
                },
            ))
