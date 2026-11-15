# v0.4.0 evidence scripts

Analysis scripts behind the v0.4.0 decisions. They are not part of the
integration and never run in Home Assistant. Each reads a COPY of the
integration's database (opened read-only) or the collector's archive.

| Script | Question | Result (release audit v0.4.0) |
|---|---|---|
| `live_confirm.py` | Do the backtest's findings show in live data? (blend vs sources, regime split, persistence, memory) | §2 |
| `radar_backtest.py` | Does wind-steered radar sampling warn better than Model B's fixed points? | §5, not built |
| `replay.py` | Replay of Model A on the stored forecasts: today's rules vs C1/C2/C3 variants | §3 |
| `learn_equiv.py` | Does the built learning code compute what the replay computed? | §4, bit-identical |
| `v040_check.py` | One blend + learning cycle of a code tree at a frozen time, for v0.3.6 vs v0.4.0 | §4 |
| `pop_check.py` | Is the published rain probability calibrated? | §5, C7 dropped |

`wrap.py <tree> <script> [args]` loads the test suite's Home Assistant
stubs for the two scripts that import the integration.

**February 2027:** rerun `replay.py` on a winter database copy to decide
on C1 (the clear-calm regime), which was deferred.
