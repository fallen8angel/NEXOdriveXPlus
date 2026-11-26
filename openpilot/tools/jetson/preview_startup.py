"""Offline production-renderer preview. No vehicle, USB or measured Jetson GPU."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('output', type=Path)
  args = parser.parse_args()
  out = args.output.resolve()
  out.mkdir(parents=True, exist_ok=True)
  root = Path(__file__).resolve().parents[3]
  sys.path.insert(0, str(root / 'openpilot/selfdrive/carrot/cluster'))
  import cluster_renderer
  from cluster_models import SimulatorInput
  from cluster_simulator import ClusterSimulator
  from openpilot.tools.jetson.gear_view import install
  from openpilot.tools.jetson.startup_animation import StartupAnimation, decode, claim
  from PIL import Image, ImageDraw, ImageFont
  import psutil
  import imageio_ffmpeg
  from functools import partial

  source = {}
  install(cluster_renderer, lambda: source, lambda _: '--:--:--')
  ui = cluster_renderer.ClusterUiRenderer(width=1920, height=462)
  ui.open(hidden=True)
  # Synthetic stationary P input, with no invented live fuel/TPMS/trip data.
  state = replace(ClusterSimulator().update(SimulatorInput(), .1), onroad=True, gear_text='P',
                  fuel_gauge=0., energy_gauge_label='fuel', tpms=None)
  mono, samples, frames = [0.], [], []
  process = psutil.Process()
  rss_before = process.memory_info().rss
  start_cpu = time.process_time()
  with tempfile.TemporaryDirectory() as temporary:
    player = StartupAnimation(clock=lambda: mono[0], claim_cycle=partial(claim, path=Path(temporary) / 'played.db'))
    player.future.result(timeout=10)
    try:
      for tick in range(90):
        mono[0] = tick / 20
        stamp = 100 + tick / 20
        source.update(session='preview', sent=stamp, updated=time.monotonic(),
                      mono={'carState': int(stamp*1e9)}, valid={'carState': True}, alive={'carState': True},
                      gear_view=dict(gear='park', sample_ns=int(stamp*1e9), can_ok=True,
                                     park_enabled=True, left=False, right=False),
                      hud_startup=dict(on=True, cycle='a'*32, age=3.+mono[0], safe=True))
        normal = ui.render_to_rgba_bytes(state)
        if tick < 2:
          continue  # existing two-sample gear validation
        started = time.perf_counter()
        result = player.compose(normal, source, source)
        samples.append((time.perf_counter()-started)*1000)
        frames.append(Image.frombytes('RGBA', (result[1], result[2]), bytes(result[0])).convert('RGB'))
      park = Image.frombytes('RGBA', (normal[1], normal[2]), bytes(normal[0])).convert('RGB')
      park.save(out / '04-existing-park.png')
      for index, name in [(0, '01-hyundai-logo'), (32, '02-headlamps'), (40, '03-animation-end')]:
        frames[index].save(out / (name + '.png'))
      previews = [frame.resize((960, 231), Image.Resampling.LANCZOS) for frame in frames]
      previews[0].save(out / 'startup-to-park.gif', save_all=True, append_images=previews[1:], duration=50, loop=0)
      writer = imageio_ffmpeg.write_frames(str(out / 'startup-to-park.mp4'), (1920, 462), fps=20,
                                           codec='libx264', pix_fmt_in='rgb24', pix_fmt_out='yuv420p',
                                           macro_block_size=2, output_params=['-crf', '18'])
      writer.send(None)
      for frame in frames:
        writer.send(frame.tobytes())
      writer.close()
      font = ImageFont.truetype(str(root / 'openpilot/selfdrive/assets/fonts/KaiGenGothicKR-Bold.ttf'), 25)
      comparison = Image.new('RGB', (1920, 608), '#111827')
      draw = ImageDraw.Draw(comparison)
      draw.text((24, 14), '수정 전: 기존 P 화면 / 수정 후: 원본 GIF → 같은 P 화면', font=font, fill='white')
      comparison.paste(park.resize((960, 231)), (0, 60))
      comparison.paste(frames[32].resize((960, 231)), (960, 60))
      comparison.paste(frames[40].resize((960, 231)), (0, 321))
      comparison.paste(park.resize((960, 231)), (960, 321))
      draw.text((24, 567), '실제 HUD 렌더러의 오프라인 합성 · 차량/USB 실촬영 아님 · 영상 반복은 미리보기 용도', font=font, fill='#cbd5e1')
      comparison.save(out / 'before-after.png')
      decoded, ends = decode()
      metrics = dict(platform=sys.platform, physical_hud_verified=False, gpu_measured=False,
                     size=[1920, 462], gif_duration_s=ends[-1], transition_s=.3,
                     compositor_ms_median=statistics.median(samples), compositor_ms_max=max(samples),
                     compositor_ms_p95=sorted(samples)[int(len(samples)*.95)],
                     process_cpu_s_including_renderer=time.process_time()-start_cpu,
                     process_rss_delta_bytes=process.memory_info().rss-rss_before,
                     rss_note='Includes stored preview images/video generation; not deployment RSS.',
                     decoded_asset_bytes=sum(f.width*f.height*4 for f in decoded),
                     compositor_cache_bytes=1920*462*4,
                     preview_note='20 FPS offline preview; production HUD remains 10 FPS. No GPU inference added.')
      (out / 'preview-metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
      print(json.dumps(metrics))
    finally:
      player.close()
      ui.close()


if __name__ == '__main__':
  main()
