# Multi-Agent Crane Automation — MUA600

Decentralized multi-agent control system for a crane-based manufacturing cell.
Target grade: **A** (R1–R5 satisfied).

---

## Quick Start

### 1. Install dependencies
```
pip install -r requirements.txt
```

### 2a. Run with mock simulation (no hardware needed)
```
python main.py --mock
```

### 2b. Run with the real University West crane simulation
Start the simulation executable first, then:
```
python main.py
```

### 3. LLM Planner demo (R5)
Set your Anthropic API key, then:
```
set ANTHROPIC_API_KEY=sk-ant-...
python main.py --mock --order "make 3 type-1 parts and 2 type-2 parts"
```

---

## Interactive Commands

| Command | Effect |
|---|---|
| `gen1 [N]` | Generate N type-1 parts (Source1 → Process1 → Sink) |
| `gen2 [N]` | Generate N type-2 parts (Source2 → Process2 → Process1 → Sink) |
| `fail PROCESS1` | Trigger failure at Process1 (R4 demo) |
| `clear PROCESS1` | Clear failure and restore Process1 |
| `order "text"` | Run LLM planner with natural-language input (R5) |
| `status` | Show all registered agents and sink count |
| `quit` | Stop the system |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    LLM Planner (R5)                 │
│  Natural language → JSON production plan            │
└──────────────────────┬──────────────────────────────┘
                       │ dispatch orders
          ┌────────────▼──────────────┐
          │    Source1 / Source2      │  generates PartAgents
          └────────────┬──────────────┘
                       │ spawns
     ┌─────────────────▼──────────────────────────┐
     │                 PartAgent                  │
     │  Owns process plan, drives entire workflow │
     │  Queries DF for locations and alternatives │
     └──────┬──────────────────────────┬──────────┘
            │ REQUEST transport        │ REQUEST process
     ┌──────▼──────┐           ┌──────▼───────────┐
     │  CraneAgent │           │  Process1/Process2│
     │  (coords    │           │  (capabilities,  │
     │   only)     │           │   can fail — R4) │
     └──────┬──────┘           └──────────────────┘
            │ Modbus TCP
     ┌──────▼──────────┐
     │   Simulation /  │
     │  MockModbus     │
     └─────────────────┘
```

### Agents

| Agent | Role |
|---|---|
| **CraneAgent** | Moves crane via Modbus; knows ONLY (x,y) coordinates |
| **SourceAgent** | Generates PartAgents with the correct process plan |
| **PartAgent** | Owns the process plan; coordinates transport + processing |
| **ProcessAgent** | Executes manufacturing operations; supports failure injection |
| **SinkAgent** | Accepts completed parts; tracks throughput |
| **DirectoryFacilitator** | Capability-based discovery registry (singleton) |
| **LLMPlanner** | Natural-language order intake → structured JSON plan |

### Communication

All inter-agent communication uses the **Contract Net Protocol** via the
`MessageBus` singleton (thread-safe mailboxes per agent).

Performatives: `CFP · PROPOSE · ACCEPT · REJECT · INFORM · FAILURE · REQUEST · AGREE`

---

## Configuration (R3 — Plug & Produce)

### `config/stations.json`
Defines physical coordinates and capabilities.  **Swap positions here** to
demonstrate R3 reconfigurability — no code changes required.

```json
"Process1": { "x": 300, "y": 500, "capabilities": ["process_op1", "process_op2"] }
```

> **Important**: Update x/y values to match your simulation's coordinate system.
> Refer to the Modbus reference page on Canvas.

### `config/modbus_map.json`
Maps agent names to Modbus register addresses.  Update these to match the
simulation's actual register layout (Canvas → Simulation & Modbus reference).

