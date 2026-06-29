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

---

## Grading Requirements

| Req | Grade | What to demo |
|---|---|---|
| R1 | E | `gen1 3` — three type-1 parts, all reach Sink |
| R2 | D | `gen1 2` + `gen2 2` — both types concurrent |
| R3 | C | Swap x-values of Process1/Process2 in `stations.json`, restart, `gen2 1` |
| R4 | B | `gen1 1`, then during processing: `fail PROCESS1` — part reroutes to Process2 |
| R5 | A | `order "make 3 type-1 and 2 type-2 parts"` — LLM produces JSON, simulation runs |

---

## Sample Q&A for the Oral Examination

**Q: If I add a new product type, what files change? Would you touch the Crane?**
A: Only `config/stations.json` (add a process plan) and possibly a new source entry.
The CraneAgent has zero knowledge of product types and requires no changes.

**Q: Where does the process plan for type 2 live?**
A: In `config/stations.json` under `process_plans["2"]`, loaded at startup by
SourceAgent and passed to each PartAgent at creation.  The Crane never sees it.

**Q: How does the Part detect a process failure?**
A: ProcessAgent sends `FAILURE` performative when the mock's fail flag is set
(or when the done_di register never arrives within the timeout).  PartAgent then
calls `_find_alternative(capability, exclude=failed_station)` which queries the
DirectoryFacilitator for another active station with the same capability.

**Q: What if both processes fail?**
A: `_find_alternative` returns `None`; the PartAgent logs the error and aborts
gracefully with a status message.  Other parts continue unaffected.

**Q: What does the LLM do that a hand-written planner couldn't?**
A: Accept free-form natural language ("give me three of the first kind and a couple
of the second") without explicit keyword matching.  The LLM handles varied phrasing,
implicit quantities, and can explain its reasoning.  A hand-written parser would need
every phrasing enumerated.

**Q: What if the LLM produces invalid output?**
A: `_parse_and_validate` checks structure and types; invalid output triggers a retry
(up to `max_retries`).  After all retries fail the planner returns `None` and the
system prints an error — no undefined Modbus writes occur.
