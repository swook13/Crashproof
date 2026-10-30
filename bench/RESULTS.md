Generated parts: 40 (seeds 1000..1039); analysis time per program median 717 ms, max 1545 ms.

| Mistake | Harmful injections | Detected (STOP + right finding) | Rate | Harmless injections | Correct GO |
|---|---:|---:|---:|---:|---:|
| g98_to_g99 | 24 | 24 | 100% | 16 | 16 |
| drop_tlo | 40 | 40 | 100% | 0 | 0 |
| wrong_h_shorter | 40 | 40 | 100% | 0 | 0 |
| rapid_plunge | 40 | 40 | 100% | 0 | 0 |
| spindle_not_started | 40 | 40 | 100% | 0 | 0 |
| depth_typo | 40 | 40 | 100% | 0 | 0 |
| low_safe_z | 24 | 24 | 100% | 16 | 16 |
| wrong_tool_in_pocket | 40 | 40 | 100% | 0 | 0 |
| tool_too_short | 24 | 24 | 100% | 16 | 16 |
| clamp_moved | 40 | 40 | 100% | 0 | 0 |
| **all** | **352** | **352** | **100.0%** | **48** | **48** |

Clean programs flagged STOP (false alarms): 0 / 40

Harmful vs harmless is decided by plain geometry on the setup sheet and the cycle block (bench/defects.py `harmful`), not by the simulator. Drill-travel mistakes are harmful when a travel move between holes crosses a clamp below its top; a too-short tool is harmful when the holder face goes below the stock top or one pass is deeper than the flute length (per-pass depth from the CAM setup sheet). An earlier version of this rule used total depth instead of per-pass depth and wrongly counted two-pass slots as harmful; it was corrected after inspecting those cases.
