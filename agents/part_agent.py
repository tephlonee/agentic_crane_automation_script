"""
agents/part_agent.py — the agent that owns and drives one physical part's journey.

ROLE IN THE SYSTEM:
  Every part that enters the cell gets its own PartAgent.  This agent:
    1. Reads the part's process plan (a list of stations and actions).
    2. Translates the plan into a flat ROUTE — a list of crane instructions.
    3. Sends the entire route to the CraneAgent in a single message.
    4. Waits for the crane to complete the entire route.
    5. Handles failures (R4): if a process station fails mid-route, the agent
       queries the Directory Facilitator for an alternative and re-routes.
    6. Last resort: if all recovery attempts fail, ships the part to the Sink.

ROUTE FORMAT:
  A route is a flat list of step dicts:
    {"action": "pick",    "station": "Source1",  "x": 55}
    {"action": "place",   "station": "Process1", "x": 450}
    {"action": "process", "station": "Process1"}
    {"action": "pick",    "station": "Process1", "x": 450}
    {"action": "place",   "station": "Sink",     "x": 945}

  The crane executes these in strict order without interruption.
  This guarantees the part is never abandoned mid-journey.

CNP (Contract Net Protocol):
  PartAgent → CraneAgent:   REQUEST execute_route → AGREE → INFORM/FAILURE
  CraneAgent → ProcessAgent: REQUEST process       → AGREE → INFORM/FAILURE
  (The second CNP exchange happens inside the crane while it executes the route)

FAILURE RECOVERY FLAG:
  _MULTI_STATION_EXCLUSION = True:
    All previously-tried stations are excluded from the DF query for alternatives.
    Prevents the crane from cycling back to an already-failed station.

  _MULTI_STATION_EXCLUSION = False:
    Only the station that just failed is excluded (simpler single-exclude demo).
    Use this for presentations where at most one station fails at a time.
"""

import logging

from agents.base_agent import BaseAgent
from agents.directory_facilitator import DirectoryFacilitator
from core.message import Message, Performative

logger = logging.getLogger(__name__)

# Effectively "no timeout" during testing — change back to reasonable values for production
_AGREE_TIMEOUT = 86400.0   # how long to wait for AGREE from crane (24 hours = no timeout)
_ROUTE_TIMEOUT = 86400.0   # how long to wait for final INFORM/FAILURE from crane

# ── TOGGLE THIS FLAG for presentation ────────────────────────────────────────
# True  → accumulate all failed stations in the exclusion set (prevents loops)
# False → only exclude the station that just failed (standard single-exclude)
_MULTI_STATION_EXCLUSION = True
# ─────────────────────────────────────────────────────────────────────────────


class PartAgent(BaseAgent):

    def __init__(self, agent_id: str, part_type: int,
                 process_plan: list, start_station: str):
        """
        agent_id      — unique name like "Part_Source1_2"
        part_type     — 1 or 2 (determines which process plan is used)
        process_plan  — list of steps from stations.json, e.g.:
                        [{"station":"Process1","action":"process","capability":"process_op1"},
                         {"station":"Sink",    "action":"sink"}]
        start_station — where the part currently is (the source that created it)
        """
        super().__init__(agent_id)

        self.part_type     = part_type
        self.process_plan  = list(process_plan)   # copy — this agent owns its plan
        self.start_station = start_station         # e.g. "Source1"
        self.completed     = False                 # set True when plan finishes successfully
        self._on_done      = None                  # callback set by SourceAgent to know when we finish

    # ------------------------------------------------------------------
    # Entry point — runs in PartAgent's background thread
    # ------------------------------------------------------------------

    def _run(self):
        """
        The part's entire lifecycle lives here.

        try/finally guarantees that _on_done is ALWAYS called when this thread
        exits — whether the part completed, failed, or was dumped.  SourceAgent
        uses this callback to know it is safe to start the next queued part.
        """
        plan_summary = [s["station"] for s in self.process_plan]
        self.logger.info("Starting — type %d at %s  plan=%s",
                         self.part_type, self.start_station, plan_summary)
        print(f"  [PART ] {self.agent_id} (type {self.part_type}) "
              f"plan: {plan_summary}")

        try:
            ok, _ = self._execute_plan(self.start_station, self.process_plan)
            self.completed = ok
            if ok:
                self.logger.info("COMPLETED")
                print(f"  [PART ] {self.agent_id} COMPLETED")
            else:
                self.logger.error("FAILED")
                print(f"  [PART ] {self.agent_id} FAILED")
        finally:
            # This block runs no matter what — even if an exception was raised above.
            # SourceAgent._on_part_done() will be called and _active will be set False,
            # allowing the next queued part to be created.
            if callable(self._on_done):
                self._on_done(self)

    # ------------------------------------------------------------------
    # Plan execution — sends one route to crane and waits for the result
    # ------------------------------------------------------------------

    def _execute_plan(self, start_station: str, plan_steps: list,
                      already_failed: set = None) -> tuple:
        """
        Build a route from start_station through plan_steps, send it to the crane,
        and wait for INFORM (success) or FAILURE.

        Parameters:
          start_station  — where the part physically is right now
          plan_steps     — remaining steps from the process plan
          already_failed — set of station names that have already failed in this
                           recovery chain; passed through so _recover can exclude all of them

        Returns: (success: bool, last_known_part_location: str)
        """
        # Step 1: Convert the plan steps into a flat list of crane actions
        route = self._build_route(start_station, plan_steps)

        if not route:
            # No movements needed (edge case: part already at final destination)
            last = plan_steps[-1]["station"] if plan_steps else start_station
            return True, last

        # Step 2: Find the crane in the Directory Facilitator
        crane_id = self._get_crane()
        self.logger.info("Sending route (%d steps) to crane", len(route))

        # Step 3: Send the REQUEST message to the crane
        self.send(Message(
            performative=Performative.REQUEST,
            sender=self.agent_id,
            receiver=crane_id,
            content={
                "action":  "execute_route",   # tells crane what kind of request this is
                "part_id": self.agent_id,      # identifies which part this route is for
                "route":   route,              # the list of pick/place/process steps
            },
        ))

        # Step 4: Wait for AGREE — crane acknowledges it received the route
        # (First call of the two-call CNP pattern: REQUEST → AGREE → INFORM/FAILURE)
        agree = self.receive_from(crane_id, timeout=_AGREE_TIMEOUT)
        if agree is None or agree.performative != Performative.AGREE:
            self.logger.error("No AGREE from crane")
            return False, start_station

        # Step 5: Wait for the final result — INFORM (done) or FAILURE (problem)
        # This can take a long time because the crane must complete the entire route
        # including waiting for process stations to finish.
        result = self.receive_from(crane_id, timeout=_ROUTE_TIMEOUT)
        if result is None:
            self.logger.error("Crane result timeout")
            return False, start_station

        if result.performative == Performative.INFORM:
            # Route completed successfully.
            # Find the last "place" station in the route to know where the part ended up.
            last_place = next(
                (s["station"] for s in reversed(route) if s["action"] == "place"),
                start_station,
            )
            return True, last_place

        # Step 6: Crane reported FAILURE — extract details and attempt recovery (R4)
        failed_at = result.content.get("failed_at")   # which station failed
        part_at   = result.content.get("part_at") or start_station  # where the part is now
        self.logger.warning("Route FAILED  failed_at=%s  part_at=%s", failed_at, part_at)

        if failed_at:
            # Hand off to the recovery logic (R4)
            return self._recover(failed_at, part_at, plan_steps,
                                 already_failed=already_failed)

        # No failed_at information — we don't know what went wrong; dump to sink
        self._dump_to_sink(part_at)
        return False, "Sink"

    # ------------------------------------------------------------------
    # Route builder — converts process plan → flat list of crane actions
    # ------------------------------------------------------------------

    def _build_route(self, start_station: str, plan_steps: list) -> list:
        """
        Translate the declarative process plan into imperative crane steps.

        The process plan says WHERE the part needs to go and WHAT should happen there.
        The route says HOW the crane should move: pick up here, place there, wait for process.

        Algorithm:
          Iterate through plan steps.  For each step, if the part is NOT already at
          that station, add a pick (from current location) and a place (at the station).
          If the action is "process", add a process step after the place.
          "sink" needs no extra step — the place IS the delivery.

        Example — type-1 part, start=Source1:
          plan: [{Process1, process}, {Sink, sink}]
          route:
            pick Source1 (x=55)
            place Process1 (x=450)
            process Process1
            pick Process1 (x=450)
            place Sink (x=945)
        """
        route = []
        prev  = start_station   # tracks where the part currently is

        for step in plan_steps:
            station = step["station"]   # where this step happens
            action  = step["action"]    # "process" or "sink"

            # If part is not already at this station, add transport steps
            if prev != station:
                route.append({
                    "action":  "pick",
                    "station": prev,
                    "x":       self._get_location(prev)["x"],   # look up X coordinate from DF
                })
                route.append({
                    "action":  "place",
                    "station": station,
                    "x":       self._get_location(station)["x"],
                })

            # If the action at this station is "process", add a process wait step
            if action == "process":
                route.append({"action": "process", "station": station})
            # "sink" action needs no additional step — the place step IS the delivery

            prev = station   # part is now at this station

        return route

    # ------------------------------------------------------------------
    # R4 — failure recovery
    # ------------------------------------------------------------------

    def _recover(self, failed_station: str, part_at: str,
                 original_plan: list, already_failed: set = None) -> tuple:
        """
        Attempt to re-route around a failed station (requirement R4).

        Recovery path:
          1. Build the exclusion set (all stations tried so far that failed).
          2. Query DF for an active alternative with the same capability.
          3. Rebuild the remaining plan replacing the failed station with the alternative.
          4. Call _execute_plan() with the new plan.
          5. If THAT also fails, dump to Sink.

        Returns: (success: bool, last_known_part_location: str)

        _MULTI_STATION_EXCLUSION flag:
          True  — all previously-failed stations are excluded in subsequent queries.
                  Prevents endless loops when both Process1 and Process2 are down.
          False — only the station that just failed is excluded (simpler demo mode).
        """
        # Build the set of stations to exclude from the DF query
        if _MULTI_STATION_EXCLUSION:
            excluded = set(already_failed or set())   # start from all previously-failed
            excluded.add(failed_station)               # add the one that just failed
        else:
            excluded = {failed_station}               # only exclude the immediate failure

        self.logger.info("Recovery  failed=%s  excluded=%s", failed_station, excluded)

        # Find which step in the plan corresponds to the failed station
        for i, step in enumerate(original_plan):
            if step["station"] != failed_station:
                continue   # not the right step — keep looking

            capability = step.get("capability", "process_op")   # what skill is needed

            # Ask the DF: "who else can do this, excluding all failed stations?"
            alt = self._find_alternative(capability, exclude=excluded)

            if alt is None:
                # No alternative exists — give up and dump to sink
                self.logger.warning(
                    "No active alternative for '%s' (excluded=%s) — dumping to Sink",
                    capability, excluded,
                )
                self._dump_to_sink(part_at)
                return False, "Sink"

            self.logger.info("R4 reroute: %s → %s (failed)", failed_station, alt)

            # Rebuild the remaining plan with the alternative station
            new_plan = list(original_plan)
            new_plan[i] = {**step, "station": alt}   # replace failed station with alternative

            # Re-submit the modified plan to the crane (passes excluded set so any further
            # failure also knows not to try the already-failed stations)
            ok, new_part_at = self._execute_plan(part_at, new_plan[i:],
                                                 already_failed=excluded)
            if not ok:
                # The alternative also failed — dump to sink
                self.logger.warning(
                    "Alternative %s also failed — dumping to Sink", alt
                )
                self._dump_to_sink(new_part_at)
                return False, "Sink"

            return True, new_part_at   # recovery succeeded

        # failed_station was not found in the plan (shouldn't happen in normal operation)
        self.logger.error("Failed station %s not found in plan — dumping to Sink",
                          failed_station)
        self._dump_to_sink(part_at)
        return False, "Sink"

    # ------------------------------------------------------------------
    # Last-resort sink dump
    # ------------------------------------------------------------------

    def _dump_to_sink(self, part_at: str):
        """
        Send the part directly to the Sink without any further processing.

        Called when EVERY recovery path has been exhausted.
        Builds a minimal two-step route (pick from current location, place at Sink)
        and sends it to the crane as a new execute_route request.

        The part is not wasted on the factory floor — it is collected at the sink
        even if it was not fully processed.
        """
        if part_at == "Sink":
            self.logger.info("Part already at Sink — no dump needed")
            return

        self.logger.warning(
            "Dumping %s to Sink from %s (all process paths exhausted)",
            self.agent_id, part_at,
        )
        print(f"  [PART ] {self.agent_id} ⚠  dumping to Sink "
              f"(all recoveries failed, part was at {part_at})")

        try:
            from_x = self._get_location(part_at)["x"]   # current location's X coordinate
            sink_x = self._get_location("Sink")["x"]     # sink's X coordinate
        except RuntimeError as exc:
            self.logger.error("Cannot dump — location lookup failed: %s", exc)
            return

        # Minimal two-step route: pick the part up and place it at the sink
        route = [
            {"action": "pick",  "station": part_at, "x": from_x},
            {"action": "place", "station": "Sink",  "x": sink_x},
        ]

        crane_id = self._get_crane()
        self.send(Message(
            performative=Performative.REQUEST,
            sender=self.agent_id,
            receiver=crane_id,
            content={"action": "execute_route", "part_id": self.agent_id, "route": route},
        ))

        # Wait for AGREE then INFORM (same CNP pattern as _execute_plan)
        agree = self.receive_from(crane_id, timeout=_AGREE_TIMEOUT)
        if agree is None or agree.performative != Performative.AGREE:
            self.logger.error("Crane did not accept dump route")
            return

        result = self.receive_from(crane_id, timeout=_ROUTE_TIMEOUT)
        if result is not None and result.performative == Performative.INFORM:
            self.logger.info("Dumped at Sink")
        else:
            self.logger.error("Dump to Sink failed — part left at %s", part_at)

    # ------------------------------------------------------------------
    # Directory Facilitator helpers
    # ------------------------------------------------------------------

    def _get_crane(self) -> str:
        """
        Look up the crane's agent_id in the DF.

        We never hardcode "Crane" — we ask the DF "who has capability 'crane'?"
        This satisfies R3: agents are discovered dynamically.
        """
        cranes = DirectoryFacilitator().query_capability("crane")
        if not cranes:
            raise RuntimeError("No crane registered in DF")
        return cranes[0]   # return the first (and only) crane agent ID

    def _get_location(self, station_name: str) -> dict:
        """
        Look up a station's physical coordinates from the DF.

        This satisfies R1: the crane never knows station names, only coordinates.
        PartAgent translates station names → coordinates and puts only the
        coordinates into the route.
        """
        loc = DirectoryFacilitator().get_location(station_name)
        if loc is None:
            raise RuntimeError(f"Location unknown for '{station_name}'")
        return loc   # returns {"x": int, "y": int}

    def _find_alternative(self, capability: str, exclude) -> str:
        """
        Query the DF for an active station with the given capability,
        excluding any stations in the 'exclude' set.

        capability — the skill needed (e.g. "process_op1")
        exclude    — a single station name (str) or a set of names to skip.
                     Accepts both so it works with _MULTI_STATION_EXCLUSION=False
                     (single string) and True (a set).

        Returns the first matching agent_id, or None if none found.
        """
        excluded_set = {exclude} if isinstance(exclude, str) else set(exclude)
        candidates   = DirectoryFacilitator().query_capability(capability, active_only=True)
        candidates   = [c for c in candidates if c not in excluded_set]
        self.logger.info("Alternatives for '%s'  excluded=%s  found=%s",
                         capability, excluded_set, candidates)
        return candidates[0] if candidates else None
