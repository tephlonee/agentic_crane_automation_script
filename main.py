"""
main.py — entry point for the Multi-Agent Crane Automation system.

Usage:
  python main.py --mock                          # run with simulated Modbus
  python main.py --mock --order "3 type-1 and 2 type-2 parts"
  python main.py                                 # connect to real simulation
  python main.py --config config/stations.json --modbus-map config/modbus_map.json

Interactive commands (when no --order given):
  gen1 [N]         generate N type-1 parts (default 1)
  gen2 [N]         generate N type-2 parts (default 1)
  fail PROCESS1    trigger failure at Process1 (R4 demo)
  fail PROCESS2    trigger failure at Process2
  clear PROCESS1   clear failure and bring station back online
  order "text"     run LLM planner with natural language input (R5)
  status           show registered agents and sink count
  quit / q         stop
"""

import argparse
import json
import logging
import sys
import time

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("pymodbus").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Imports (after logging config)
# ---------------------------------------------------------------------------

from agents.directory_facilitator import DirectoryFacilitator
from agents.crane_agent   import CraneAgent
from agents.source_agent  import SourceAgent
from agents.process_agent import ProcessAgent
from agents.sink_agent    import SinkAgent
from core.message_bus     import MessageBus
from core.message         import Message, Performative
from planner.llm_planner  import LLMPlanner


def load_json(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# System bootstrap
# ---------------------------------------------------------------------------

def build_system(station_cfg: dict, modbus_map: dict, use_mock: bool):
    """Instantiate and start all agents. Return agent dict."""

    stations = station_cfg["stations"]
    plans    = station_cfg["process_plans"]

    # Station positions for the mock (vacuum pick/place sensor simulation)
    station_positions = {
        name: {"x": cfg["x"], "y": cfg["y"]}
        for name, cfg in stations.items()
        if "x" in cfg and "y" in cfg
    }

    # Modbus interface
    if use_mock:
        from modbus.mock_interface import MockModbusInterface
        mb = MockModbusInterface(modbus_map, station_positions=station_positions)
    else:
        from modbus.real_interface import RealModbusInterface
        mb = RealModbusInterface(
            host=modbus_map.get("host", "127.0.0.1"),
            port=modbus_map.get("port", 502),
        )

    if not mb.connect():
        sys.exit("ERROR: Cannot connect to Modbus. Start simulation first or use --mock.")

    # Apply per-station processing durations to mock (real simulation uses its own timers)
    if use_mock and hasattr(mb, "set_processing_time"):
        for name, cfg in stations.items():
            if "processing_time" in cfg:
                mb.set_processing_time(name, cfg["processing_time"])

    agents = {}

    # Crane
    agents["Crane"] = CraneAgent("Crane", mb, modbus_map, station_cfg)

    # Sources — pass modbus so sensors are set when parts are generated
    agents["Source1"] = SourceAgent("Source1", stations["Source1"], plans,
                                    part_type=1, modbus=mb, modbus_map=modbus_map)
    agents["Source2"] = SourceAgent("Source2", stations["Source2"], plans,
                                    part_type=2, modbus=mb, modbus_map=modbus_map)

    # Process stations
    agents["Process1"] = ProcessAgent("Process1", mb, modbus_map, stations["Process1"])
    agents["Process2"] = ProcessAgent("Process2", mb, modbus_map, stations["Process2"])

    # Sink
    agents["Sink"] = SinkAgent("Sink", stations["Sink"])

    # Start all
    for agent in agents.values():
        agent.start()

    time.sleep(2)   # give DF registrations a moment to settle
    print("\n[SYSTEM] All agents started.\n")
    return agents, mb


# ---------------------------------------------------------------------------
# LLM order dispatch
# ---------------------------------------------------------------------------

def dispatch_llm_order(order_text: str, agents: dict):
    planner = LLMPlanner()
    plan = planner.plan(order_text)

    if plan is None:
        print("[LLM] Could not produce a valid plan.")
        return

    print(f"[LLM] Plan: {json.dumps(plan, indent=2)}")

    for order in plan["orders"]:
        part_type = order["type"]
        count     = order["count"]
        source_id = f"Source{part_type}"

        if source_id not in agents:
            print(f"[LLM] No source for type {part_type}")
            continue

        print(f"[LLM] Dispatching {count}x type-{part_type} to {source_id}")
        agents[source_id].bus.send(Message(
            performative=Performative.REQUEST,
            sender="LLMPlanner",
            receiver=source_id,
            content={"action": "generate", "part_type": part_type, "count": count},
        ))


# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

def run_interactive(agents: dict, mb):
    print("Interactive mode. Type 'help' for commands.\n")

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not raw:
            continue

        # Split into cmd + everything-else as one string (preserves quoted sentences)
        parts = raw.split(None, 1)
        cmd   = parts[0].lower()
        rest  = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("quit", "q", "exit"):
            break

        elif cmd == "help":
            print(__doc__)

        elif cmd in ("gen1", "gen2"):
            ptype = 1 if cmd == "gen1" else 2
            count = int(rest) if rest else 1
            src   = f"Source{ptype}"
            for _ in range(count):
                agents[src].generate_part(ptype)
            print(f"  Queued {count} type-{ptype} part(s) at {src}")

        elif cmd == "fail" and rest:
            target = rest.split()[0]
            if target in agents and hasattr(agents[target], "trigger_failure"):
                agents[target].trigger_failure()
                print(f"  Failure triggered at {target}")
            else:
                print(f"  Unknown or non-process agent: {target}")

        elif cmd == "clear" and rest:
            target = rest.split()[0]
            if target in agents and hasattr(agents[target], "clear_failure"):
                agents[target].clear_failure()
                print(f"  Failure cleared at {target}")
            else:
                print(f"  Unknown agent: {target}")

        elif cmd == "order" and rest:
            order_text = rest.strip('"\'')
            dispatch_llm_order(order_text, agents)

        elif cmd == "status":
            df = DirectoryFacilitator()
            sink = agents.get("Sink")
            print(f"\n  Registered agents:")
            for aid, info in df.list_all().items():
                print(f"    {aid:15s}  status={info['status']:8s}  caps={info['capabilities']}")
            if sink:
                print(f"\n  Parts completed: {sink.get_count()}")
                for p in sink.get_received():
                    print(f"    {p['part_id']}  type={p['part_type']}")
            print()

        else:
            print(f"  Unknown command: {raw!r}  (type 'help')")

    print("\n[SYSTEM] Shutting down...")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Multi-Agent Crane Automation — MUA600"
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="Use in-process mock Modbus (no simulation needed)",
    )
    parser.add_argument(
        "--config", default="config/stations.json",
        help="Station configuration file",
    )
    parser.add_argument(
        "--modbus-map", default="config/modbus_map.json",
        help="Modbus register address map",
    )
    parser.add_argument(
        "--order", type=str, default=None,
        help="Natural-language production order for LLM planner (R5)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    station_cfg = load_json(args.config)
    modbus_map  = load_json(args.modbus_map)

    agents, mb = build_system(station_cfg, modbus_map, use_mock=args.mock)

    try:
        if args.order:
            # Non-interactive: run LLM plan, wait for completion
            dispatch_llm_order(args.order, agents)
            print("\n[SYSTEM] Waiting for all parts to complete (Ctrl-C to stop)...\n")
            while True:
                time.sleep(1.0)
        else:
            run_interactive(agents, mb)
    except KeyboardInterrupt:
        pass
    finally:
        print("[SYSTEM] Stopping all agents...")
        for agent in reversed(list(agents.values())):
            agent.stop()
        mb.disconnect()
        print("[SYSTEM] Done.")


if __name__ == "__main__":
    main()
