# Offline traffic-signal observer

Find horizontal signal housings, track them through consecutive RGB frames, and
write observed red/green/unknown states with an annotated video. This is a
classical image-processing experiment, not a newly trained ONNX model.

```sh
python -m pip install -r tools/signal_analysis/requirements.txt
python tools/signal_analysis/replay_signal.py input.hevc output --start-frame 400 --end-frame 609
python -m pytest tools/signal_analysis/test_signal_tracker.py -q
```

Choose a new output directory. Outputs are `observer.mp4`, `observer.jsonl`, and
`summary.json`. Frame bounds are inclusive. The default frame rate is 20 Hz;
override with `--fps`. For recorded camera timing, provide `--timestamps times.json`
containing `{ "400": 123456789000 }` mappings from frame index to EOF nanoseconds.
Without that file, temporal decisions use nominal frame index / fps.

The observer consumes current/previous images and timestamps only. It receives no
labels, manual boxes, future frames, vehicle state or driving-model outputs.
Green confirmation requires a previously confirmed red on the same track;
startup green remains unknown. Agreeing visible tracks do not identify the ego
lane, distinguish turn arrows, measure stop-line distance or authorize movement.
No vehicle messaging, Params or control outputs are implemented.

See [the upstream trial report](https://github.com/ajouatom/openpilot/blob/c6925561478f8fc84e50fce4373c87b0dd075d4a/docs/signal_tracking_trial_20261010.md)
for its reviewed clip results and limitations. These are upstream results, not
NEXO vehicle validation. Private recordings, annotations and generated videos
are intentionally not included. NEXO integration and opt-in scope are recorded
in [SIGNAL_UPSTREAM.md](../../openpilot/selfdrive/carrot/SIGNAL_UPSTREAM.md).
