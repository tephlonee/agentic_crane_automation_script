"""
agents/directory_facilitator.py — the "yellow pages" of the multi-agent system.

ROLE IN THE SYSTEM:
  The Directory Facilitator (DF) is where agents announce WHAT they can do
  and WHERE they are located.  Other agents query it to find a capable partner
  without ever needing to know the partner's name in advance.

  This is what satisfies requirement R3 (dynamic discovery) and R4 (runtime
  re-routing): when a station fails, its DF status is set to "failed".  The
  PartAgent queries the DF for any ACTIVE station with the same capability and
  gets back the alternative — completely at runtime, no hardcoded fallback.

DESIGN: Singleton
  Like the MessageBus, there is exactly ONE DirectoryFacilitator shared by all
  threads.  Every call to DirectoryFacilitator() returns the same object.

EXAMPLE FLOW:
  1. Process1 starts → registers: caps=["process_op1","process_op2"], loc={x:450,y:82}
  2. PartAgent queries: "who has capability 'process_op1'?" → ["Process1", "Process2"]
  3. Process1 fails → set_status("Process1", "failed")
  4. PartAgent queries again with active_only=True → ["Process2"]
  5. PartAgent reroutes the part to Process2.
"""

import threading    # for RLock — a re-entrant lock that allows the same thread to lock twice
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DirectoryFacilitator:
    """
    Singleton service registry: agents register capabilities, others query them.

    The registry is a dict of dicts:
        {
          "Process1": {
              "capabilities": ["process_op1", "process_op2"],
              "location":     {"x": 450, "y": 82},
              "metadata":     {"processing_time": 3.0},
              "status":       "active"    # or "failed" or "busy"
          },
          "Crane": { ... },
          ...
        }
    """

    _instance   = None
    _class_lock = threading.Lock()

    def __new__(cls):
        """Singleton: return the same object on every call to DirectoryFacilitator()."""
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._registry: Dict[str, dict] = {}   # the main registry dict
                    inst._lock = threading.RLock()          # re-entrant lock (same thread can acquire twice)
                    cls._instance = inst
        return cls._instance

    @classmethod
    def reset(cls):
        """Destroy the singleton — used in tests."""
        with cls._class_lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Registration — agents call this at startup
    # ------------------------------------------------------------------

    def register(self, agent_id: str, capabilities: List[str],
                 location: Optional[Dict] = None,
                 metadata: Optional[Dict] = None):
        """
        Add or update an agent's entry in the registry.

        agent_id     — the unique name (e.g. "Process1")
        capabilities — list of strings describing what this agent can do
                       (e.g. ["process_op1", "process_op2"])
        location     — physical position {"x": int, "y": int} used by PartAgent
                       to look up coordinates when building a crane route
        metadata     — extra info like processing_time, part_type, etc.

        Initial status is always "active" — agent is available immediately.
        """
        with self._lock:
            self._registry[agent_id] = {
                "capabilities": list(capabilities),     # copy so caller can't mutate it
                "location":     dict(location or {}),   # copy, or empty dict if None
                "metadata":     dict(metadata or {}),
                "status":       "active",               # starts as active
            }
        logger.info("[DF] Registered %s  caps=%s  loc=%s", agent_id, capabilities, location)

    def deregister(self, agent_id: str):
        """Remove an agent from the registry (called when agent shuts down)."""
        with self._lock:
            self._registry.pop(agent_id, None)
        logger.info("[DF] Deregistered %s", agent_id)

    # ------------------------------------------------------------------
    # Status management — used for failure injection (R4)
    # ------------------------------------------------------------------

    def set_status(self, agent_id: str, status: str):
        """
        Change an agent's status.  Valid values: 'active', 'failed', 'busy'.

        Called by ProcessAgent.trigger_failure() to mark a station as broken.
        Called by ProcessAgent.clear_failure() to bring it back online.

        The PartAgent checks this when querying for alternatives:
          query_capability(..., active_only=True) will skip "failed" stations.
        """
        with self._lock:
            if agent_id in self._registry:
                self._registry[agent_id]["status"] = status
        logger.info("[DF] Status of %s -> %s", agent_id, status)

    def get_status(self, agent_id: str) -> Optional[str]:
        """Return the current status string for agent_id, or None if not registered."""
        with self._lock:
            entry = self._registry.get(agent_id)
            return entry["status"] if entry else None

    # ------------------------------------------------------------------
    # Location lookup — used by PartAgent to build crane routes (R3)
    # ------------------------------------------------------------------

    def update_location(self, agent_id: str, location: Dict):
        """Update a registered agent's physical location."""
        with self._lock:
            if agent_id in self._registry:
                self._registry[agent_id]["location"] = dict(location)

    def get_location(self, agent_id: str) -> Optional[Dict]:
        """
        Return the physical location of agent_id.

        This is how PartAgent builds crane routes without hardcoding coordinates.
        It asks the DF: "where is Process1?" → {"x": 450, "y": 82}.
        The crane receives only coordinates, never station names — satisfying R1.
        """
        with self._lock:
            entry = self._registry.get(agent_id)
            return dict(entry["location"]) if entry else None  # return a copy

    # ------------------------------------------------------------------
    # Capability discovery — used for R3 and R4
    # ------------------------------------------------------------------

    def query_capability(self, capability: str,
                         active_only: bool = True) -> List[str]:
        """
        Find all agents that advertise a given capability.

        capability  — the string to search for (e.g. "process_op1", "crane", "sink")
        active_only — if True, skip agents whose status is not "active"
                      (this is what makes R4 work: failed stations are excluded)

        Returns a list of agent_id strings.  Empty list means no one available.

        Example:
          query_capability("process_op1", active_only=True)
          →  ["Process2"]   (if Process1 has status="failed")
        """
        with self._lock:
            return [
                aid for aid, info in self._registry.items()
                if capability in info["capabilities"]           # has the capability
                and (not active_only or info["status"] == "active")  # and is active if required
            ]

    def get_info(self, agent_id: str) -> Optional[Dict]:
        """Return the full registry entry for an agent (a copy)."""
        with self._lock:
            entry = self._registry.get(agent_id)
            return dict(entry) if entry else None

    def list_all(self) -> Dict:
        """Return a snapshot of the entire registry — used by the 'status' CLI command."""
        with self._lock:
            return {k: dict(v) for k, v in self._registry.items()}   # deep-ish copy
