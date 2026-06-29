"""
agents/source_agent.py — manages one source station (Source1 or Source2).

ROLE IN THE SYSTEM:
  A source is where raw parts enter the manufacturing cell.  The SourceAgent's
  job is to:
    1. Accept "generate N parts" commands from the CLI or LLM planner.
    2. Queue them so that only ONE part is active at a time.
    3. Create a PartAgent for each queued part and start it running.
    4. Wait until that PartAgent fully finishes (including any recovery or sink
       dump) before activating the next queued part.

WHY ONE AT A TIME?
  The crane processes one complete route atomically.  If two PartAgents are both
  alive and both send routes to the crane, the routes interleave in the crane's
  mailbox.  The crane would then interrupt Part1's journey (e.g. mid-recovery)
  to pick up Part2.  One-at-a-time sequencing at the source prevents this.

COMPLETION CALLBACK:
  Instead of polling or using a timer, SourceAgent registers a callback function
  (_on_part_done) with each PartAgent it creates.  PartAgent calls this function
  at the very end of its _run() method (in a try/finally so it ALWAYS fires,
  even if an exception occurs).  This is how SourceAgent knows it is safe to
  activate the next queued part.

AUTO-GENERATE (real simulation):
  The monitoring loop in _run() watches the source sensor register.  When a
  physical part appears at the source (sensor goes 0→1) due to the user clicking
  the Generate button in the simulation GUI, SourceAgent automatically creates
  a PartAgent.  No CLI command needed.
"""

import threading   # for Lock (thread-safe access to shared state)
import logging

from agents.base_agent import BaseAgent
from agents.directory_facilitator import DirectoryFacilitator
from core.message import Performative

logger = logging.getLogger(__name__)

_POLL = 0.3   # how often (seconds) to check the sensor register in the real simulation


class SourceAgent(BaseAgent):

    def __init__(self, agent_id: str, station_config: dict,
                 all_plans: dict, part_type: int,
                 modbus=None, modbus_map: dict = None):
        """
        agent_id       — "Source1" or "Source2"
        station_config — from stations.json: {"x":55, "y":82, ...}
        all_plans      — the full process_plans dict from stations.json
                         (keyed by part type as string: "1" → [...], "2" → [...])
        part_type      — which type of part this source produces (1 or 2)
        modbus         — the Modbus interface (real or mock) for writing the sensor register
        modbus_map     — the register address config so we can look up the sensor register
        """
        super().__init__(agent_id)   # sets up agent_id, bus registration, logger

        self._location   = {"x": station_config["x"], "y": station_config["y"]}
        self._all_plans  = all_plans          # e.g. {"1": [{station:Process1,...},...], "2":[...]}
        self._part_type  = part_type          # default part type for this source
        self._mb         = modbus             # Modbus interface — used to write sensor register
        self._sensor_reg = None               # will hold the register address for this source's sensor

        # Look up the sensor register address from the modbus map
        # e.g. Source1 → register 17,  Source2 → register 18
        if modbus_map and agent_id in modbus_map.get("stations", {}):
            self._sensor_reg = modbus_map["stations"][agent_id].get("sensor")

        self._counter      = 0             # increments with each part created → unique part IDs
        self._parts: list  = []            # keeps references to PartAgent objects (prevents GC)
        self._pending: list = []           # queue of part types waiting to be activated
        self._active       = False         # True while a PartAgent is running from this source
        self._lock         = threading.Lock()   # protects _pending and _active from race conditions

    # ------------------------------------------------------------------
    # Public API — called from main.py CLI and LLM planner
    # ------------------------------------------------------------------

    def generate_part(self, part_type: int = None):
        """
        Queue one part for generation.

        If no PartAgent is currently active, the part is activated immediately.
        If one is active (mid-journey, recovery, or dump), it goes into the
        _pending list and will be started when the current one finishes.

        Thread-safe: uses self._lock because CLI (main thread) and the agent's
        background thread both touch _pending and _active.
        """
        ptype = part_type or self._part_type   # use provided type or this source's default
        if str(ptype) not in self._all_plans:
            self.logger.error("No process plan for part type %d", ptype)
            return
        with self._lock:
            self._pending.append(ptype)   # add to the FIFO queue
            self.logger.info("Queued type-%d part  (pending=%d)", ptype, len(self._pending))
            if not self._active:
                self._activate_next()     # no part running → start immediately

    # ------------------------------------------------------------------
    # Completion callback — called by PartAgent when its thread exits
    # ------------------------------------------------------------------

    def _on_part_done(self, part):
        """
        Called automatically by PartAgent at the very end of its _run() method,
        whether the part succeeded, failed, or was dumped to the sink.

        This is the trigger to start the next queued part.
        It runs in the PartAgent's thread, not the SourceAgent's thread,
        which is why we use self._lock.
        """
        self.logger.info(
            "Part %s finished (completed=%s) — source free", part.agent_id, part.completed
        )
        with self._lock:
            self._active = False          # source is now free
            self._activate_next()         # start next queued part if any

    # ------------------------------------------------------------------
    # Internal — activate the next queued part
    # ------------------------------------------------------------------

    def _activate_next(self):
        """
        Pop the next part type from the pending queue and start it.

        MUST be called with self._lock already held (it doesn't acquire it).

        Steps:
          1. Pop the part type from the front of the queue.
          2. Write sensor register = 1 (tells the crane "a part is here").
          3. Start a background thread to create the PartAgent.
             (We use a thread so the lock is released quickly — PartAgent
             creation involves importing and instantiating objects.)
        """
        if not self._pending:
            return   # queue is empty — nothing to activate

        ptype = self._pending.pop(0)    # take from the FRONT (FIFO order)
        self._active = True             # mark source as busy

        # Write 1 to the source sensor register so the crane's sensor-wait check
        # sees a part is present before descending to pick it up.
        if self._mb is not None and self._sensor_reg is not None:
            self._mb.write_holding_register(self._sensor_reg, 1)

        # Create the PartAgent in a separate short-lived thread to avoid holding
        # self._lock while doing object construction work.
        threading.Thread(
            target=self._create_part, args=(ptype,), daemon=True
        ).start()

    def _create_part(self, ptype: int):
        """
        Actually instantiate and start a PartAgent.

        Runs in a short-lived background thread (spawned by _activate_next).
        The PartAgent itself then spawns its OWN thread (via start()).
        """
        from agents.part_agent import PartAgent   # deferred import avoids circular dependency

        self._counter += 1   # increment so each part gets a unique ID
        part_id = f"Part_{self.agent_id}_{self._counter}"   # e.g. "Part_Source1_2"
        plan    = self._all_plans[str(ptype)]               # look up the process plan

        part = PartAgent(
            agent_id=part_id,
            part_type=ptype,
            process_plan=plan,        # the list of steps: [{station:Process1, action:process}, ...]
            start_station=self.agent_id,   # where the part starts — this source station
        )
        # Register the completion callback BEFORE starting the thread,
        # so it is always set when _run() exits (even if _run() exits immediately).
        part._on_done = self._on_part_done

        self._parts.append(part)   # keep a reference so the object isn't garbage collected
        part.start()               # spawns PartAgent's background thread, starts _run()

        self.logger.info("Started %s (type %d)", part_id, ptype)
        print(f"  [SOURCE] {self.agent_id} → {part_id} (type {ptype}) started")

    # ------------------------------------------------------------------
    # Agent loop — runs in SourceAgent's background thread
    # ------------------------------------------------------------------

    def _run(self):
        """
        SourceAgent's main loop.  Two responsibilities:
          A. Monitor the sensor register for external triggers (real simulation).
          B. Process incoming REQUEST messages (from LLM planner).
        """
        # Register in the Directory Facilitator so other agents can find this source
        df = DirectoryFacilitator()
        df.register(
            self.agent_id,
            ["source"],                      # capability: "source"
            location=self._location,         # physical position for crane routing
            metadata={"part_type": self._part_type},  # extra info
        )
        self.logger.info("Ready  part_type=%d  loc=%s", self._part_type, self._location)

        prev_sensor = 0   # track previous sensor value to detect 0→1 transitions

        while self._running:

            # --- A. External sensor monitoring (real simulation: user clicks Generate) ---
            if self._mb is not None and self._sensor_reg is not None:
                cur = self._mb.read_holding_register(self._sensor_reg)   # read current sensor value

                if cur == 1 and prev_sensor == 0:
                    # Sensor just went HIGH from an external source.
                    # This means the physical simulation placed a part here (button click).
                    # Only create a PartAgent if no part is currently being processed
                    # AND there are no parts queued — the queue mechanism already handles
                    # any programmatically-generated parts.
                    with self._lock:
                        if not self._active and not self._pending:
                            self.logger.info(
                                "External sensor trigger — auto-creating part at %s",
                                self.agent_id,
                            )
                            self._active = True
                            threading.Thread(
                                target=self._create_part,
                                args=(self._part_type,), daemon=True
                            ).start()

                prev_sensor = cur   # remember for next iteration to detect the transition

            # --- B. Handle incoming REQUEST messages ---
            # The LLM planner sends REQUEST messages with action="generate".
            msg = self.receive(timeout=_POLL)   # block for 0.3s max, then loop
            if msg is None:
                continue   # nothing arrived — go back to the top of the loop

            if (msg.performative == Performative.REQUEST
                    and msg.content.get("action") == "generate"):
                count = int(msg.content.get("count", 1))
                ptype = int(msg.content.get("part_type", self._part_type))
                self.logger.info("Order received: %d × type-%d", count, ptype)
                for _ in range(count):
                    self.generate_part(ptype)   # queue each part individually
