# CrashProof

**The last check before Cycle Start.** CrashProof simulates a CNC milling program against the setup that is *really* on the machine (the tools in the magazine, the offsets typed into the control, the clamps on the table) and stops the job before a crash or a scrapped part. When it finds a problem, NVIDIA Nemotron on Nebius Token Factory proposes a fix, and the fix is only accepted after the simulator proves it.

> The model never decides what is safe. Physics does.

Built for the Nebius x NVIDIA Global AI Hackathon (Best Apps and Agents track).

---

## The problem

A CNC program can be perfect in the CAM system and still crash the machine. CAM simulation checks the *planned* setup. Crashes happen in the gap between the plan and the machine:

- a length offset (H) typed with a transposed digit, or a G43 line deleted during a hand edit at the control
- a different end mill loaded in the pocket the program calls
- a clamp placed where the setup sheet did not put it
- a drill cycle switched from G98 to G99 to save a few seconds, so the drill now travels between holes below the clamps

Full machine-simulation suites exist, but they need an expert to model the machine, the tools and the fixtures for every job, so small job shops run new programs with single-block and a hand on the feed-hold button. In aerospace and defense job shops (7075 and titanium brackets, mounts, housings), customer programs often cannot be sent to a third-party model provider, and a released NC program cannot be changed without showing the change does not alter the part.

## What CrashProof does

1. **Reads the real setup.** Tool table with measured lengths, H registers, work offsets, clamps and the finished-part features from the CAM setup sheet. A photo of the control's offset page can be read by an NVIDIA vision model; a tool known only by its part number can be looked up in the maker's catalog with Tavily (dimensions are accepted only when the quoted evidence is found verbatim in the source).
2. **Simulates the program twice.** Once as the machine will run it (loaded tool lengths and geometry) and once as the CAM programmer intended it (programmed coordinates, CAM tool geometry). Material removal is a height map (Z-map); the cutter, shank, holder and spindle nose are checked against the remaining material and every fixture on every move.
3. **Gives a verdict: GO or STOP**, with the first failure, its line, position and depth, and the likely cause.
4. **Repairs with proof.** Nemotron proposes three candidate fixes in parallel. Each candidate is applied and re-simulated on the real setup. A fix is accepted only if every blocking finding is gone **and** the machined shape is identical to what the original program intended (the height maps match within tolerance in every cell). If the program is right and the setup is wrong, Nemotron proposes a setup correction instead, and that is proved the same way.
5. **Builds a safe restart block** for resuming mid-program after a broken tool, and proves it against the material that is actually left at that line.
6. **Issues a run certificate**: program hash, setup hash, every check, the setup values the verdict is valid for, and the simulator's limits.

## Where Nebius and NVIDIA are used

| Step | Model / service | Why this model |
|---|---|---|
| Repair agent: diagnose and propose code or setup fixes (3 candidates per round, up to 3 rounds, with simulator feedback on rejected attempts) | **NVIDIA Nemotron 3 Super** via **Nebius Token Factory** | Reasoning over a long program listing plus setup tables; candidates are sampled in parallel at different temperatures |
| Operator briefing: what will physically happen and what to check before Cycle Start | **NVIDIA Nemotron** fast model via **Nebius Token Factory** | Short, low-latency text grounded only in the simulator's findings |
| Catalog extraction from Tavily search results | Nemotron fast model via Token Factory | Structured JSON with a verbatim evidence quote |
| Reading a photo of the control's offset page | NVIDIA vision model via Token Factory | Register values are shown to the operator as a diff before they enter the setup |
| Tool catalog search | **Tavily** | Shops know tools by part number, not by 3D model |

All model calls go through the OpenAI-compatible Token Factory endpoint (`https://api.tokenfactory.nebius.com/v1/`). Model IDs are set in `.env`. Because the models are open, the same pipeline can run on a shop's own hardware or a zero-retention endpoint when customer programs must not leave the building.

## Measured results

Detection benchmark (`python -m bench.run --parts 40`): 40 generated parts of the same family (different stock size, pocket, holes, slot, clamp positions, depths and heights), 10 kinds of shop-floor mistakes injected into each.

| Mistake | Harmful | Detected | Harmless | Correct GO |
|---|---:|---:|---:|---:|
| Drill cycle G98 → G99 | 24 | 24 | 16 | 16 |
| G43 line deleted | 40 | 40 | – | – |
| H register of a shorter tool | 40 | 40 | – | – |
| Feed move changed to rapid | 40 | 40 | – | – |
| Spindle never started | 40 | 40 | – | – |
| Depth typed 2 mm too deep | 40 | 40 | – | – |
| Safe height decimal slip (25. → 2.5) | 24 | 24 | 16 | 16 |
| Larger end mill in the pocket | 40 | 40 | – | – |
| Tool loaded with almost no stickout | 24 | 24 | 16 | 16 |
| Clamp moved over a hole | 40 | 40 | – | – |
| **All** | **352** | **352** | **48** | **48** |

Clean programs flagged STOP: **0 / 40**. Median analysis time 0.7 s per program (both simulations).

How to read this honestly:
- Whether an injected mistake is *harmful* is decided by plain geometry on the setup sheet and the cycle block (`bench/defects.py`), not by the simulator. A mistake that cannot hurt a particular part (for example G99 when no travel move crosses a clamp) should get GO, and it did.
- All parts come from one program generator written for this project. This shows the checks are consistent, not that they generalize to every CAM post processor. Testing on programs from commercial CAM systems is the next step.
- Simulator accuracy: the removed volume of a pocket matches the analytic volume within 1 % (`tests/test_core.py`).

Repair-agent results with Nemotron are recorded per scenario in `cache/` and shown in the demo as "recorded run" with their timestamp; the live button re-runs them.

## Architecture

```
G-code ──► interpreter (Fanuc-style: G0-G3, IJK/R arcs, G17, G20/21, G54-59, G43/G49, G81-G89, G28, G53, M6)
              │  moves of the spindle gauge line in machine coordinates + active H value
              ▼
setup ──► simulator ── real mode:  tip = gauge − loaded tool length   ──► findings (line, position, depth)
              │        ideal mode: tip = gauge − H value (CAM intent) ──► intended part shape
              ▼
          verdict GO / STOP ──► Nemotron proposes fixes ──► simulator proves or rejects each one
```

| File | What it does |
|---|---|
| `crashproof/gcode.py` | G-code interpreter |
| `crashproof/sim.py` | Z-map material removal, collision checks, gouge check |
| `crashproof/core.py` | analysis, patch application, proof, restart block, certificate |
| `crashproof/agent.py` | Nemotron repair loop and operator briefing |
| `crashproof/research.py` | Tavily catalog lookup, control-screen reading |
| `web/` | FastAPI backend and the operator UI (three.js) |
| `bench/` | mistake injectors, ground truth, benchmark |
| `examples/` | example part generator, synthetic control-screen image |

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # add NEBIUS_API_KEY (and TAVILY_API_KEY for catalog lookup)
python examples/make_examples.py
uvicorn web.app:app --port 8765
# open http://localhost:8765
```

Without an API key the simulator, verdicts, restart blocks and certificates all work; the repair loop replays recorded Nemotron runs.

Tests and benchmark:

```bash
python -m pytest tests
python -m bench.run --parts 40
```

## Limits

- 3-axis milling only, G17 arcs. Cutter radius compensation (G41/G42), macros and subprograms are reported as not simulated.
- Fixtures and holders are boxes and cylinders; the finished part is described by pockets, holes and slots from the setup sheet.
- The control-screen example image in `examples/` is synthetic and marked as such.
- A GO verdict is only valid for the setup values printed on the certificate.

## Related work

- CGTech VERICUT, NCSIMUL, CAMotics and CAM-integrated simulators verify toolpaths; CrashProof's contribution is building the check from the machine's actual state and proving model-proposed repairs, not the simulator itself.
- *Few-Shot VLM-Based G-Code and HMI Verification* (arXiv 2512.11296) uses a vision-language model to judge G-code against the machine screen. CrashProof deliberately keeps the safety judgment out of the model.
- LLM G-code error detection for additive manufacturing (arXiv 2309.02465) and GLLM (LLM G-code generation with verification).

## License

Apache License 2.0
