# Demo transcripts

Real output recorded on 26 October 2026 against Hindsight Cloud, for anyone
evaluating without an API key. Box-drawn panels are the 21 × 8 OLED card on the
glasses.

| File | Command | Shows |
|---|---|---|
| [`demo-compare.txt`](demo-compare.txt) | `python demo/hindsight_demo.py` | Day 20, five moments, memory OFF vs ON, with the facts Hindsight used and the latency |
| [`demo-curve.txt`](demo-curve.txt) | `python demo/hindsight_demo.py --curve` | The same three moments on day 1, 5 and 20 as memory grows |
| [`demo-guide.txt`](demo-guide.txt) | `python demo/hindsight_demo.py --guide` | The tea guide planned on day 1 (9 steps), 5 (cues) and 20 (4 steps) |
| [`demo-cpp.txt`](demo-cpp.txt) | `echo-demo --text --headless-hud [--hindsight]` | The C++ core: abstains without memory, answers from it with |

**Story version.** These runs used the first version of
[`demo/story.py`](../../echo-esp32-firmware/demo/story.py), in which three dates were
written as fixed calendar days ("Friday the 6th", "the 11th", "Wednesday the 28th").
The story now derives them from the run date so they stay consistent with the
back-dated timestamps whenever it is run. That's why the day-1 card here mentions a
play on "Friday 6th". Re-running produces the same behaviour with different dates.

Memory answers come from Hindsight reflect, so wording varies a little between runs;
the facts don't.
