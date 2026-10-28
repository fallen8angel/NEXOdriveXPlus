"""The software FFmpeg HUD must honor the same physical mirror bit as native NV12."""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cluster'))
import cluster_renderer as cr


@pytest.mark.parametrize('flip,expected', [(False, 0), (True, 1)])
def test_portrait_rgba_mirror_flips_the_upload_vertical_axis(monkeypatch, flip, expected):
  calls = []
  image = SimpleNamespace(width=462, height=1920)
  texture = SimpleNamespace(width=1920, height=462)
  target = SimpleNamespace(texture=texture)
  fake = SimpleNamespace(
    hidden=True,
    width=1920,
    height=462,
    open=lambda **kwargs: None,
    _profile_start=lambda: 0,
    _profile_add=lambda *args: None,
    _get_capture_target=lambda: target,
    render=lambda state: None,
    _flush_pending_stroked_text_textures=lambda: None,
    _publish_web_mirror_target=lambda value: None,
    _get_portrait_upload_target=lambda *args: target,
    _current_theme=lambda: SimpleNamespace(bg='dark'),
  )
  rl = SimpleNamespace(
    begin_texture_mode=lambda value: None,
    end_texture_mode=lambda: None,
    clear_background=lambda color: None,
    draw_texture_pro=lambda *args: None,
    load_image_from_texture=lambda value: image,
    image_flip_vertical=lambda value: calls.append(value),
    Rectangle=lambda *args: args,
    Vector2=lambda *args: args,
  )
  monkeypatch.setattr(cr, 'rl', rl)
  monkeypatch.setattr(cr, 'rl_color', lambda value: value)

  assert cr.ClusterUiRenderer._render_to_image(fake, object(), portrait_upload=True, flip_x=flip) is image
  assert len(calls) == expected
  assert not calls or calls[0] is image


def test_ffmpeg_rgba_path_passes_live_mirror_bit_to_capture():
  # Static AST guard: this keyword used to be missing on the software-only
  # render_to_rgba_buffer call even though native NV12 already honored it.
  path = Path(__file__).resolve().parents[1] / 'cluster/main.py'
  tree = ast.parse(path.read_text())
  matches = []
  for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'render_to_rgba_buffer':
      args = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
      if 'portrait_upload' in args and 'h264_portrait_upload' in args['portrait_upload']:
        matches.append(args)
  assert len(matches) == 1
  assert matches[0]['flip_x'] == 'not bool(active_hud_mirror_mode & 1)'
