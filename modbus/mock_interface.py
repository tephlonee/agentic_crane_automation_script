"""
MockModbusInterface — in-process simulation of the crane cell.

All signals are holding registers, exactly as the real simulation.
The background thread simulates:
  - Crane position tracking (pos_x/pos_y moving toward set_x/set_y)
  - Vacuum pick/place transitions (clears/sets station sensor registers)
  - Process timing (is_running 1 -> 0 after the configured duration)
  - Failure injection (stops an active process early)
"""

import threading
import time
import logging
from typing import Dict

from modbus.interface import ModbusInterface

logger = logging.getLogger(__name__)

_SIM_TICK    = 0.05    # simulation step in seconds (20 Hz)
_CRANE_SPEED = 200     # register-units per tick  (~4000 units/sec)


class MockModbusInterface(ModbusInterface):

    def __init__(self, modbus_map: dict, station_positions: dict = None):
        """
        station_positions: {station_name: {"x": int, "y": int}}
        Used to determine which station the crane is at during vacuum transitions.
        """
        self._cmap     = modbus_map["crane"]
        self._smap     = modbus_map.get("stations", {})
        self._positions = station_positions or {}

        # Single register space — all holding registers
        self._hrs  = [0] * 200
        self._lock = threading.RLock()

        # Per-station process state (duration overridden later via set_processing_time)
        self._proc: Dict[str, dict] = {
            name: {"active": False, "start_t": 0.0, "duration": 3.0, "failed": False}
            for name in self._smap
        }

        self._running = False
        self._sim_thread: threading.Thread = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        self._running = True
        self._sim_thread = threading.Thread(
            target=self._simulate, daemon=True, name="MockModbus"
        )
        self._sim_thread.start()
        logger.info("[Mock] Simulation started")
        return True

    def disconnect(self):
        self._running = False
        logger.info("[Mock] Simulation stopped")

    # ------------------------------------------------------------------
    # Register access (holding registers only)
    # ------------------------------------------------------------------

    def read_holding_register(self, address: int) -> int:
        with self._lock:
            return self._hrs[address]

    def write_holding_register(self, address: int, value: int) -> bool:
        with self._lock:
            self._hrs[address] = value
        return True

    # Unused in this simulation — kept for interface compliance
    def read_coil(self, address: int) -> bool:
        return False

    def write_coil(self, address: int, value: bool) -> bool:
        return True

    def read_discrete_input(self, address: int) -> bool:
        return False

    # ------------------------------------------------------------------
    # External helpers used by SourceAgent and ProcessAgent
    # ------------------------------------------------------------------

    def set_processing_time(self, station_name: str, duration: float):
        """Override simulated processing duration for a station."""
        if station_name in self._proc:
            self._proc[station_name]["duration"] = duration

    def set_source_sensor(self, station_name: str, value: int = 1):
        """Simulate a part appearing at a source station."""
        sensor_reg = self._smap.get(station_name, {}).get("sensor")
        if sensor_reg is not None:
            with self._lock:
                self._hrs[sensor_reg] = value
            logger.debug("[Mock] Sensor %s = %d", station_name, value)

    def trigger_station_failure(self, station_name: str):
        with self._lock:
            if station_name in self._proc:
                self._proc[station_name]["failed"] = True
        logger.warning("[Mock] FAILURE injected at %s", station_name)

    def clear_station_failure(self, station_name: str):
        with self._lock:
            if station_name in self._proc:
                self._proc[station_name]["failed"] = False
                self._proc[station_name]["active"] = False
                is_running_reg = self._smap.get(station_name, {}).get("is_running")
                run_reg        = self._smap.get(station_name, {}).get("run")
                if is_running_reg is not None:
                    self._hrs[is_running_reg] = 0
                if run_reg is not None:
                    self._hrs[run_reg] = 0
        logger.info("[Mock] Failure cleared at %s", station_name)

    # ------------------------------------------------------------------
    # Background simulation loop
    # ------------------------------------------------------------------

    def _simulate(self):
        cmap        = self._cmap
        set_x_reg   = cmap["set_x"]
        set_y_reg   = cmap["set_y"]
        pos_x_reg   = cmap["pos_x"]
        pos_y_reg   = cmap["pos_y"]
        vacuum_reg  = cmap["vacuum"]
        prev_vacuum = 0

        while self._running:
            time.sleep(_SIM_TICK)
            now = time.time()

            with self._lock:
                # ---- Crane movement ----
                for actual, target in ((pos_x_reg, set_x_reg), (pos_y_reg, set_y_reg)):
                    a, t = self._hrs[actual], self._hrs[target]
                    if a != t:
                        step = min(_CRANE_SPEED, abs(t - a))
                        self._hrs[actual] = a + step if t > a else a - step

                # ---- Vacuum transitions → sensor updates ----
                cur_vacuum = self._hrs[vacuum_reg]
                if cur_vacuum != prev_vacuum:
                    cx = self._hrs[pos_x_reg]
                    cy = self._hrs[pos_y_reg]
                    if cur_vacuum == 1:
                        self._on_grab(cx, cy)
                    else:
                        self._on_release(cx, cy)
                    prev_vacuum = cur_vacuum

                # ---- Process station timing ----
                for name, state in self._proc.items():
                    is_running_reg = self._smap.get(name, {}).get("is_running")
                    run_reg        = self._smap.get(name, {}).get("run")
                    if is_running_reg is None or run_reg is None:
                        continue

                    # Start process if run=1 and not already active
                    if (self._hrs[run_reg] == 1
                            and not state["active"]
                            and self._hrs[is_running_reg] == 0
                            and not state["failed"]):
                        state["active"]  = True
                        state["start_t"] = now
                        self._hrs[is_running_reg] = 1
                        logger.debug("[Mock] Process started at %s", name)

                    if not state["active"]:
                        continue

                    # Failure mid-process: stop immediately
                    if state["failed"]:
                        state["active"] = False
                        self._hrs[is_running_reg] = 0
                        self._hrs[run_reg]        = 0
                        logger.warning("[Mock] Process at %s aborted by failure", name)
                        continue

                    # Normal completion
                    if now - state["start_t"] >= state["duration"]:
                        state["active"] = False
                        self._hrs[is_running_reg] = 0
                        self._hrs[run_reg]        = 0
                        logger.debug("[Mock] Process done at %s", name)

    # ------------------------------------------------------------------
    # Sensor bookkeeping on vacuum transitions
    # ------------------------------------------------------------------

    _POSITION_TOL = 5   # units — how close the crane must be to a station

    def _station_at(self, cx: int, cy: int):
        """Return the name of the station the crane is currently at, or None."""
        for name, pos in self._positions.items():
            if (abs(pos["x"] - cx) <= self._POSITION_TOL
                    and abs(pos["y"] - cy) <= self._POSITION_TOL):
                return name
        return None

    def _is_source_station(self, name: str) -> bool:
        """Source stations have a sensor but no run/is_running registers."""
        smap = self._smap.get(name, {})
        return "sensor" in smap and "run" not in smap

    def _on_grab(self, cx: int, cy: int):
        """Vacuum activated — clear sensor only for source stations.
        Process stations are not cleared because a subsequent part may already
        be sitting there, and clearing would cause a false 'sensor not ready'
        warning for the next processing cycle."""
        name = self._station_at(cx, cy)
        if name is not None and self._is_source_station(name):
            sensor_reg = self._smap.get(name, {}).get("sensor")
            if sensor_reg is not None:
                self._hrs[sensor_reg] = 0
                logger.debug("[Mock] Pick-up at %s → sensor cleared", name)

    def _on_release(self, cx: int, cy: int):
        """Vacuum released — set the sensor at the crane's current position."""
        name = self._station_at(cx, cy)
        if name is not None:
            sensor_reg = self._smap.get(name, {}).get("sensor")
            if sensor_reg is not None:
                self._hrs[sensor_reg] = 1
                logger.debug("[Mock] Place at %s → sensor set", name)
