#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import time

import av

import openpilot.cereal.messaging as messaging

V4L2_BUF_FLAG_KEYFRAME = 8
DEFAULT_RESULT_PORT = 8769
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(SCRIPT_DIR, "best.pt")


def log(message: str) -> None:
  # Prefix every pipeline event with epoch time so diagnostics can distinguish
  # a live stream from an old/stalled log entry.
  print(f"{time.time():.3f} {message}", flush=True)


def main() -> int:
  parser = argparse.ArgumentParser(description="Direct Jetson roadEncodeData -> HEVC -> YOLO pipeline")
  parser.add_argument("addr", help="comma IP where cereal bridge publishes roadEncodeData")
  parser.add_argument("--model", default=DEFAULT_MODEL)
  parser.add_argument("--conf", type=float, default=0.25)
  parser.add_argument("--imgsz", type=int, default=640)
  parser.add_argument("--device", default="0")
  parser.add_argument("--skip", type=int, default=2, help="run YOLO every N decoded frames")
  parser.add_argument("--conflate", action="store_true")
  parser.add_argument("--result-port", type=int, default=DEFAULT_RESULT_PORT)
  parser.add_argument("--result-host", default="255.255.255.255")
  parser.add_argument("--print-every", type=int, default=1)
  args = parser.parse_args()

  from ultralytics import YOLO

  model_path = os.path.abspath(os.path.expanduser(args.model))
  if not os.path.exists(model_path):
    raise FileNotFoundError(f"YOLO model not found: {model_path}")
  model = YOLO(model_path)

  os.environ["ZMQ"] = "1"
  messaging.reset_context()
  sock = messaging.sub_sock("roadEncodeData", None, addr=args.addr, conflate=args.conflate)

  codec = av.CodecContext.create("hevc", "r")
  seen_iframe = False
  last_encode_id = -1
  frame_cnt = 0
  yolo_cnt = 0

  result_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  result_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

  log(f"[orin-direct] subscribing roadEncodeData from {args.addr}")
  log(
    f"[orin-direct] model={model_path} conf={args.conf} imgsz={args.imgsz} "
    f"device={args.device} skip={args.skip}"
  )

  while True:
    msgs = messaging.drain_sock(sock, wait_for_one=True)
    for evt in msgs:
      evta = getattr(evt, evt.which())
      encode_id = int(evta.idx.encodeId)

      if last_encode_id != -1 and encode_id != last_encode_id + 1:
        log(f"[orin-direct] DROP? encodeId {last_encode_id} -> {encode_id}")
      last_encode_id = encode_id

      if not seen_iframe:
        if not (int(evta.idx.flags) & V4L2_BUF_FLAG_KEYFRAME):
          continue
        try:
          codec.decode(av.packet.Packet(bytes(evta.header)))
        except Exception as e:
          log(f"[orin-direct] header decode error: {e}")
          continue
        seen_iframe = True
        log("[orin-direct] got first iframe/header")

      try:
        frames = codec.decode(av.packet.Packet(bytes(evta.data)))
      except Exception as e:
        log(f"[orin-direct] decode error: {e}")
        continue

      if not frames:
        continue

      frame = frames[0]
      img_bgr = frame.to_ndarray(format="bgr24")
      frame_cnt += 1

      # Repeat this event periodically so diagnostics can distinguish a live
      # decode stream from a frame that was only seen once at startup.
      if frame_cnt == 1 or (frame_cnt % 100) == 0:
        log(f"[orin-direct] frame decoded count={frame_cnt} shape={img_bgr.shape}")

      if args.skip > 1 and (frame_cnt % args.skip) != 0:
        continue

      t0 = time.perf_counter()
      results = model.predict(
        source=img_bgr,
        imgsz=args.imgsz,
        conf=args.conf,
        device=args.device,
        verbose=False,
      )
      infer_ms = (time.perf_counter() - t0) * 1000.0
      yolo_cnt += 1

      result = results[0]
      boxes = result.boxes
      det_n = 0 if boxes is None else len(boxes)

      if args.print_every <= 1 or (yolo_cnt % args.print_every) == 0:
        log(f"[orin-direct] encodeId={encode_id} det={det_n} infer={infer_ms:.1f}ms")

      objects = []
      if boxes is not None and det_n > 0:
        names = result.names
        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        classes = boxes.cls.cpu().numpy().astype(int)
        h, w = img_bgr.shape[:2]

        for (x1, y1, x2, y2), conf, cls_id in zip(xyxy, confs, classes):
          name = names.get(int(cls_id), str(int(cls_id))) if isinstance(names, dict) else str(int(cls_id))
          objects.append({
            "class_id": int(cls_id),
            "name": name,
            "conf": round(float(conf), 4),
            "x1": round(float(x1), 1),
            "y1": round(float(y1), 1),
            "x2": round(float(x2), 1),
            "y2": round(float(y2), 1),
          })

        width = int(w)
        height = int(h)
      else:
        height, width = img_bgr.shape[:2]

      payload = {
        "magic": "NEXO_JETSON_YOLO",
        "version": 1,
        "ts": time.time(),
        "pipeline": "direct_roadEncodeData",
        "encode_id": encode_id,
        "width": int(width),
        "height": int(height),
        "infer_ms": round(infer_ms, 2),
        "objects": objects,
      }
      data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

      targets = {(args.result_host, args.result_port), (args.addr, args.result_port)}
      for target in targets:
        try:
          result_sock.sendto(data, target)
        except Exception:
          pass


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except KeyboardInterrupt:
    raise SystemExit(0)
