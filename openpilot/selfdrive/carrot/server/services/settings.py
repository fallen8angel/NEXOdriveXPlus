import json
import os
from typing import Any, Dict, List, Tuple

from ..config import DEFAULT_SETTINGS_PATH


# mtime-based cache for carrot_settings.json
# - "path" is mutated by carrot_server.py at startup if --settings is passed
# - "mtime" tracks the last loaded file mtime so reload happens only on change
settings_cache: dict = {
  "path": DEFAULT_SETTINGS_PATH,
  "mtime": 0,
  "data": None,        # full json
  "groups": None,      # {group: [param,...]}
  "by_name": None,     # {name: param}
  "groups_list": None, # [{group, egroup, count}, ...]
  "categories": None,  # 대>중>소 트리 ([{id,ko,en,zh,groups:[...]}]) or None when no "menu"
}


NEXO_EXPERIMENTAL_SWITCH_SPEED = {
  "group": "가속설정",
  "name": "NexoExperimentalSwitchSpeed",
  "title": "실험모드 전환속도",
  "descr": "NEXO 실험모드 기준속도입니다. 설정값을 중심으로 2km/h 여유를 두어 모드가 반복 전환되지 않게 합니다. 예: 20 설정 → 22km/h 이상 일반모드, 18km/h 이하 실험모드",
  "egroup": "ACCEL",
  "etitle": "NEXO Experimental Mode Switch Speed",
  "edescr": "Base speed for NEXO Experimental Mode. A 2 km/h hysteresis prevents rapid mode toggling.",
  "cgroup": "加速设置",
  "ctitle": "NEXO 实验模式切换速度",
  "cdescr": "NEXO 实验模式基准速度。使用 2km/h 回差避免模式频繁切换。",
  "min": 10,
  "max": 100,
  "default": 20,
  "unit": 5,
  "display_unit": "speedKph",
}


NEXO_HUD_SIDE_CAMERA_SETTINGS = (
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCamera",
    "title": "사각지대 카메라(외부 HUD)",
    "descr": "콤마4 실내 카메라의 좌우 영역을 외부 HUD에 크게 표시합니다. 0: 끄기, 1: 켜기. 차량 제어에는 관여하지 않는 표시 기능입니다.",
    "egroup": "CLUSTER HUD",
    "etitle": "Blind Spot Camera (External HUD)",
    "edescr": "Shows cropped left/right areas of the comma 4 driver camera on the external HUD. Display only; it does not affect vehicle control.",
    "cgroup": "外部 HUD",
    "ctitle": "盲区摄像头（外部 HUD）",
    "cdescr": "在外部 HUD 上显示 comma 4 驾驶员摄像头的左右裁剪区域。仅显示，不参与车辆控制。",
    "min": 0, "max": 1, "default": 0, "unit": 1, "control": "select",
    "options": {"ko": ["끄기", "켜기"], "en": ["Off", "On"], "zh": ["关闭", "开启"]},
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraTrigger",
    "title": "사각지대 카메라 작동 조건",
    "descr": "0: 방향지시등, 1: 사각지대 경고(BSM), 2: 둘 다. 비상등처럼 좌우 방향지시등이 동시에 켜진 경우에는 방향지시등만으로 카메라를 띄우지 않습니다.",
    "egroup": "CLUSTER HUD",
    "etitle": "Blind Spot Camera Trigger",
    "edescr": "0: turn signal, 1: blind-spot warning (BSM), 2: either. Hazard lights alone do not open the camera.",
    "cgroup": "外部 HUD",
    "ctitle": "盲区摄像头触发条件",
    "cdescr": "0: 转向灯, 1: 盲区警告(BSM), 2: 两者。双闪灯本身不会打开摄像头。",
    "min": 0, "max": 2, "default": 2, "unit": 1, "control": "select",
    "options": {"ko": ["방향지시등", "BSM 위험 감지", "둘 다"], "en": ["Turn signal", "BSM warning", "Either"], "zh": ["转向灯", "BSM 警告", "两者"]},
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraPreview",
    "title": "사각지대 카메라 설정 미리보기",
    "descr": "주차(P) 상태와 1km/h 이하에서만 미리보기를 강제로 표시합니다. 0: 끄기, 1: 좌측, 2: 우측, 3: 좌우. 위치와 확대율을 조정한 뒤 반드시 0으로 돌려두세요.",
    "egroup": "CLUSTER HUD",
    "etitle": "Blind Spot Camera Setup Preview",
    "edescr": "For calibration, forces a preview only in Park at 1 km/h or below. Return this to Off after setup.",
    "cgroup": "外部 HUD",
    "ctitle": "盲区摄像头设置预览",
    "cdescr": "仅在 P 挡且车速不超过 1km/h 时强制预览。设置完成后请恢复为关闭。",
    "min": 0, "max": 3, "default": 0, "unit": 1, "control": "select",
    "options": {"ko": ["끄기", "좌측", "우측", "좌우"], "en": ["Off", "Left", "Right", "Both"], "zh": ["关闭", "左侧", "右侧", "左右"]},
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraLeftX",
    "title": "좌측 카메라 중심 위치",
    "descr": "실내 카메라 영상에서 좌측 사각지대로 사용할 가로 중심 위치입니다. 외부 HUD 미리보기를 보면서 조정하세요.",
    "egroup": "CLUSTER HUD", "etitle": "Left Camera Center X",
    "edescr": "Horizontal center used for the left blind-spot crop. Adjust while watching the external HUD preview.",
    "cgroup": "外部 HUD", "ctitle": "左侧摄像头中心位置", "cdescr": "左侧盲区裁剪的水平中心位置。",
    "min": 0, "max": 100, "default": 25, "unit": 5, "display_unit": "percent",
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraRightX",
    "title": "우측 카메라 중심 위치",
    "descr": "실내 카메라 영상에서 우측 사각지대로 사용할 가로 중심 위치입니다. 외부 HUD 미리보기를 보면서 조정하세요.",
    "egroup": "CLUSTER HUD", "etitle": "Right Camera Center X",
    "edescr": "Horizontal center used for the right blind-spot crop. Adjust while watching the external HUD preview.",
    "cgroup": "外部 HUD", "ctitle": "右侧摄像头中心位置", "cdescr": "右侧盲区裁剪的水平中心位置。",
    "min": 0, "max": 100, "default": 75, "unit": 5, "display_unit": "percent",
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraY",
    "title": "사각지대 카메라 세로 중심",
    "descr": "좌우 사각지대 화면에 공통으로 적용되는 세로 중심 위치입니다.",
    "egroup": "CLUSTER HUD", "etitle": "Blind Spot Camera Center Y",
    "edescr": "Shared vertical center for both side-camera crops.",
    "cgroup": "外部 HUD", "ctitle": "盲区摄像头垂直中心", "cdescr": "左右裁剪共用的垂直中心。",
    "min": 0, "max": 100, "default": 50, "unit": 5, "display_unit": "percent",
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraZoom",
    "title": "사각지대 카메라 확대",
    "descr": "100%는 넓게 보고 값이 커질수록 선택한 좌우 영역을 더 크게 확대합니다. 좌우 화면에 공통 적용됩니다.",
    "egroup": "CLUSTER HUD", "etitle": "Blind Spot Camera Zoom",
    "edescr": "100% is the widest view; higher values zoom further into the selected side. Shared by left and right.",
    "cgroup": "外部 HUD", "ctitle": "盲区摄像头缩放", "cdescr": "100% 为最宽视野，数值越大放大越多。左右共用。",
    "min": 100, "max": 300, "default": 180, "unit": 10, "display_unit": "percent",
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraWidth",
    "title": "사각지대 카메라 화면 가로 크기",
    "descr": "외부 HUD 위에 겹쳐 표시되는 좌우 카메라 박스의 가로 크기입니다. 좌우 화면에 공통 적용되며 20~50% 범위에서 조절할 수 있습니다.",
    "egroup": "CLUSTER HUD", "etitle": "Blind Spot Camera Panel Width",
    "edescr": "Width of the left/right camera overlay panel on the external HUD. Shared by both sides and adjustable from 20% to 50%.",
    "cgroup": "外部 HUD", "ctitle": "盲区摄像头画面宽度", "cdescr": "外部 HUD 上左右摄像头叠加框的宽度。左右共用，可在 20% 到 50% 之间调整。",
    "min": 20, "max": 50, "default": 39, "unit": 1, "display_unit": "percent",
  },
  {
    "group": "외부 HUD",
    "name": "ClusterHudSideCameraHeight",
    "title": "사각지대 카메라 화면 세로 크기",
    "descr": "외부 HUD 위에 겹쳐 표시되는 좌우 카메라 박스의 세로 크기입니다. 좌우 화면에 공통 적용되며 40~95% 범위에서 조절할 수 있습니다.",
    "egroup": "CLUSTER HUD", "etitle": "Blind Spot Camera Panel Height",
    "edescr": "Height of the left/right camera overlay panel on the external HUD. Shared by both sides and adjustable from 40% to 95%.",
    "cgroup": "外部 HUD", "ctitle": "盲区摄像头画面高度", "cdescr": "外部 HUD 上左右摄像头叠加框的高度。左右共用，可在 40% 到 95% 之间调整。",
    "min": 40, "max": 95, "default": 93, "unit": 1, "display_unit": "percent",
  },
)


def _inject_nexo_hud_side_camera(data: Dict[str, Any]) -> None:
  params = data.setdefault("params", [])
  existing = {p.get("name") for p in params}
  for setting in NEXO_HUD_SIDE_CAMERA_SETTINGS:
    if setting["name"] not in existing:
      params.append(dict(setting))

  names = [setting["name"] for setting in NEXO_HUD_SIDE_CAMERA_SETTINGS]

  def visit(nodes: List[Dict[str, Any]]) -> bool:
    for node in nodes:
      if node.get("id") == "DISP_HUD":
        groups = node.setdefault("groups", [])
        target = next((group for group in groups if group.get("id") == "HUD_SIDE_CAMERA"), None)
        if target is None:
          groups.append({
            "id": "HUD_SIDE_CAMERA",
            "ko": "사각지대 카메라",
            "en": "Blind Spot Camera",
            "zh": "盲区摄像头",
            "params": list(names),
          })
        else:
          target["params"] = list(names)
        return True
      if visit(node.get("groups") or []):
        return True
    return False

  visit(data.get("menu") or [])


def _inject_nexo_experimental_switch_speed(data: Dict[str, Any]) -> None:
  """Expose the NEXO Experimental/Normal speed threshold in the web settings.

  Keep carrot_settings.json upstream-compatible by injecting this NEXO-only
  extension at load time instead of maintaining a large JSON fork.
  """
  params = data.setdefault("params", [])
  name = NEXO_EXPERIMENTAL_SWITCH_SPEED["name"]
  if not any(p.get("name") == name for p in params):
    params.append(dict(NEXO_EXPERIMENTAL_SWITCH_SPEED))

  def visit(nodes: List[Dict[str, Any]]) -> bool:
    for node in nodes:
      if node.get("id") == "CRUISE_DRIVE_MODE":
        items = node.setdefault("params", [])
        if name not in items:
          try:
            idx = items.index("MyDrivingModeAuto") + 1
          except ValueError:
            idx = len(items)
          items.insert(idx, name)
        return True
      if visit(node.get("groups") or []):
        return True
    return False

  visit(data.get("menu") or [])


def read_settings_file(path: str) -> Dict[str, Any]:
  with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
  _inject_nexo_experimental_switch_speed(data)
  _inject_nexo_hud_side_camera(data)
  return data


def group_index(settings: Dict[str, Any]) -> Tuple[Dict[str, list], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
  groups: Dict[str, list] = {}
  by_name: Dict[str, Dict[str, Any]] = {}
  groups_list: List[Dict[str, Any]] = []

  params = settings.get("params", [])
  for p in params:
    g = p.get("group", "기타")
    if g == "기타":
        if "egroup" not in p: p["egroup"] = "Other"
        if "cgroup" not in p: p["cgroup"] = "其他"

    groups.setdefault(g, []).append(p)
    n = p.get("name")
    if n:
      by_name[n] = p

  # group list with egroup/cgroup guess
  for g, items in groups.items():
    egroup = None
    cgroup = None
    for it in items:
      if not egroup and it.get("egroup"):
        egroup = it.get("egroup")
      if not cgroup and it.get("cgroup"):
        cgroup = it.get("cgroup")
      if egroup and cgroup:
        break
    groups_list.append({"group": g, "egroup": egroup, "cgroup": cgroup, "count": len(items)})

  return groups, by_name, groups_list


def _label(node: Dict[str, Any]) -> Dict[str, Any]:
  return {"ko": node.get("ko"), "en": node.get("en"), "zh": node.get("zh")}


def build_menu_categories(data: Dict[str, Any], by_name: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]] | None:
  """Build the 대>중>소 tree from the optional top-level "menu" block.

  Returns None when no "menu" is present so the frontend falls back to the
  flat group_index view. Leaf "items" carry param *names* only; the frontend
  resolves definitions from items_by_group to avoid duplicating param data.

  Shape:
    [ {id, ko, en, zh,
       groups: [ {id, ko, en, zh, count,
                  sections: [ {id, ko, en, zh, items:[name,...]} ]} ]} ]

  A 중-group whose menu node holds params directly (no nested "groups") is
  normalized to a single label-less section.
  """
  menu = data.get("menu")
  if not menu:
    return None

  def join_labels(nodes: List[Dict[str, Any]], key: str) -> str | None:
    parts = [str(n.get(key) or "").strip() for n in nodes]
    parts = [p for p in parts if p]
    return " · ".join(parts) if parts else None

  def sections_from(nodes: List[Dict[str, Any]], parents: List[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []
    parents = parents or []
    for node in nodes:
      path = parents + [node]
      children = node.get("groups") or []
      if children:
        sections.extend(sections_from(children, path))
        continue
      items = [n for n in node.get("params", []) if n in by_name]
      if not items:
        continue
      sections.append({
        "id": "__".join(str(n.get("id") or "") for n in path if n.get("id")),
        "ko": join_labels(path, "ko"),
        "en": join_labels(path, "en"),
        "zh": join_labels(path, "zh"),
        "items": items,
      })
    return sections

  cats: List[Dict[str, Any]] = []
  for cat in menu:
    groups_out: List[Dict[str, Any]] = []
    for grp in cat.get("groups", []):
      if "groups" in grp:
        sections = sections_from(grp["groups"])
      else:
        # params directly under the 중-group → single label-less section
        sections = [{"id": grp.get("id"), "ko": None, "en": None, "zh": None,
                     "items": [n for n in grp.get("params", []) if n in by_name]}]
      count = sum(len(s["items"]) for s in sections)
      groups_out.append({**_label(grp), "id": grp.get("id"), "count": count, "sections": sections})
    cats.append({**_label(cat), "id": cat.get("id"), "groups": groups_out})
  return cats


def get_settings_cached() -> Tuple[Dict[str, Any], Dict[str, list], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
  path = settings_cache["path"]
  st = os.stat(path)
  mtime = int(st.st_mtime)
  if settings_cache["data"] is None or settings_cache["mtime"] != mtime:
    data = read_settings_file(path)
    groups, by_name, groups_list = group_index(data)
    settings_cache.update({
      "mtime": mtime,
      "data": data,
      "groups": groups,
      "by_name": by_name,
      "groups_list": groups_list,
      "categories": build_menu_categories(data, by_name),
    })
  return (
    settings_cache["data"],
    settings_cache["groups"],
    settings_cache["by_name"],
    settings_cache["groups_list"],
  )
