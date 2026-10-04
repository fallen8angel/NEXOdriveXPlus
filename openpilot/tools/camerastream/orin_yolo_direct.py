#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import time
from typing import Any

import av

import openpilot.cereal.messaging as messaging

V4L2_BUF_FLAG_KEYFRAME = 8
DEFAULT_RESULT_PORT = 8769
DEFAULT_STATUS_FILE = "/tmp/nexo-yolo-direct-status.json"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(SCRIPT_DIR, "best.pt")


def log(message: str) -> None:
  # Epoch timestamps let diagnostics distinguish live output from stale logs.
  print(f"{time.time():.3f} {message}", flush=True)


def write_status(path: str, state: dict[str, Any], **updates: Any) -> None:
  state.update(updates)
  state["updated_ts"] = time.time()
  tmp = f"{path}.{os.getpid()}.tmp"
  try:
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
  except Exception:
    try:
      os.unlink(tmp)
    except OSError:
      pass


def make_decoder() -> av.CodecContext:
  # Match carrot-wip compressed_vipc.py: roadEncodeData carries HEVC and the
  # decoder must not start until a V4L2 keyframe/header pair has arrived.
  return av.CodecContext.create("hevc", "r")


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
  parser.add_argument("--status-file", default=os.environ.get("NEXO_YOLO_STATUS_FILE", DEFAULT_STATUS_FILE))
  args = parser.parse_args()

  from ultralytics import YOLO

  model_path = os.path.abspath(os.path.expanduser(args.model))
  if not os.path.exists(model_path):
    raise FileNotFoundError(f"YOLO model not found: {model_path}")

  status: dict[str, Any] = {
    "magic": "NEXO_YOLO_DIRECT_STATUS",
    "version": 2,
    "mode": "direct",
    "source": "carrot_compressed_vipc",
    "pid": os.getpid(),
    "started_ts": time.time(),
    "comma_ip": args.addr,
    "model": model_path,
    "state": "loading_model",
    "iframe_seen": False,
    "frame_seen": False,
    "yolo_recent": False,
    "packets_seen": 0,
    "keyframes_seen": 0,
    "decoder_resets": 0,
  }
  write_status(args.status_file, status)

  model = YOLO(model_path)
  write_status(args.status_file, status, state="subscribing")

  # This is intentionally the same transport pattern used by carrot-wip's
  # compressed_vipc.py. The Jetson subscribes to comma's roadEncodeData ZMQ
  # stream directly instead of adding a local frame-bridge/pipe layer.
  os.environ["ZMQ"] = "1"
  messaging.reset_context()
  sock = messaging.sub_sock("roadEncodeData", None, addr=args.addr, conflate=args.conflate)

  codec = make_decoder()
  seen_iframe = False
  last_encode_id = -1
  frame_cnt = 0
  packet_cnt = 0
  keyframe_cnt = 0
  decoder_resets = 0
  yolo_cnt = 0
  last_frame_status_write = 0.0
  last_wait_status_write = 0.0

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
      flags = int(evta.idx.flags)
      is_keyframe = bool(flags & V4L2_BUF_FLAG_KEYFRAME)
      packet_cnt += 1
      packet_now = time.time()

      if packet_cnt == 1:
        write_status(
          args.status_file,
          status,
          state="waiting_iframe",
          first_packet_ts=packet_now,
          last_packet_ts=packet_now,
          packets_seen=packet_cnt,
          last_encode_id=encode_id,
          last_flags=flags,
        )

      if last_encode_id != -1 and encode_id != last_encode_id + 1:
        log(f"[orin-direct] DROP? encodeId {last_encode_id} -> {encode_id}")
      last_encode_id = encode_id

      if not seen_iframe:
        if not is_keyframe:
          if packet_now - last_wait_status_write >= 1.0:
            write_status(
              args.status_file,
              status,
              state="waiting_iframe",
              iframe_seen=False,
              frame_seen=False,
              yolo_recent=False,
              last_packet_ts=packet_now,
              packets_seen=packet_cnt,
              keyframes_seen=keyframe_cnt,
              decoder_resets=decoder_resets,
              last_encode_id=encode_id,
              last_flags=flags,
              last_error="",
            )
            last_wait_status_write = packet_now
          continue

        keyframe_cnt += 1
        try:
          # carrot-wip feeds evta.header first on the initial V4L2 keyframe.
          # Without this HEVC VPS/SPS/PPS data PyAV can receive packets but
          # never produce the first decoded surface.
          codec.decode(av.packet.Packet(bytes(evta.header)))
        except Exception as e:
          log(f"[orin-direct] header decode error: {e}")
          codec = make_decoder()
          decoder_resets += 1
          write_status(
            args.status_file,
            status,
            state="header_decode_error",
            iframe_seen=False,
            last_packet_ts=packet_now,
            packets_seen=packet_cnt,
            keyframes_seen=keyframe_cnt,
            decoder_resets=decoder_resets,
            last_encode_id=encode_id,
            last_flags=flags,
            last_error=str(e),
          )
          continue

        seen_iframe = True
        log("[orin-direct] got first iframe/header")
        write_status(
          args.status_file,
          status,
          state="decoding",
          iframe_seen=True,
          first_iframe_ts=packet_now,
          last_packet_ts=packet_now,
          packets_seen=packet_cnt,
          keyframes_seen=keyframe_cnt,
          decoder_resets=decoder_resets,
          last_encode_id=encode_id,
          last_flags=flags,
          last_error="",
        )

      try:
        frames = codec.decode(av.packet.Packet(bytes(evta.data)))
      except Exception as e:
        log(f"[orin-direct] decode error: {e}; resyncing at next iframe")
        # A dropped/corrupt HEVC dependency can leave the decoder unable to
        # recover. Re-enter carrot's keyframe gate and rebuild the decoder so
        # the next iframe/header pair starts from a known-good state.
        codec = make_decoder()
        seen_iframe = False
        decoder_resets += 1
        write_status(
          args.status_file,
          status,
          state="resync_wait_iframe",
          iframe_seen=False,
          frame_seen=False,
          yolo_recent=False,
          last_packet_ts=packet_now,
          packets_seen=packet_cnt,
          keyframes_seen=keyframe_cnt,
          decoder_resets=decoder_resets,
          last_encode_id=encode_id,
          last_flags=flags,
          last_error=str(e),
        )
        continue

      if not frames:
        continue

      frame = frames[0]
      img_bgr = frame.to_ndarray(format="bgr24")
      frame_cnt += 1
      now = time.time()

      if frame_cnt == 1:
        log(f"[orin-direct] frame decoded count={frame_cnt} shape={img_bgr.shape}")

      if frame_cnt == 1 or now - last_frame_status_write >= 0.5:
        h, w = img_bgr.shape[:2]
        write_status(
          args.status_file,
          status,
          state="frame_streaming",
          iframe_seen=True,
          frame_seen=True,
          last_packet_ts=packet_now,
          last_frame_ts=now,
          packets_seen=packet_cnt,
          keyframes_seen=keyframe_cnt,
          decoder_resets=decoder_resets,
          last_encode_id=encode_id,
          last_flags=flags,
          width=int(w),
          height=int(h),
          frame_count=frame_cnt,
          last_error="",
        )
        last_frame_status_write = now

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

      yolo_now = time.time()
      write_status(
        args.status_file,
        status,
        state="running",
        iframe_seen=True,
        frame_seen=True,
        yolo_recent=True,
        last_packet_ts=packet_now,
        last_frame_ts=yolo_now,
        last_yolo_ts=yolo_now,
        packets_seen=packet_cnt,
        keyframes_seen=keyframe_cnt,
        decoder_resets=decoder_resets,
        last_encode_id=encode_id,
        last_flags=flags,
        last_det=det_n,
        last_infer_ms=round(infer_ms, 2),
        width=int(width),
        height=int(height),
        frame_count=frame_cnt,
        yolo_count=yolo_cnt,
        last_error="",
      )

      payload = {
        "magic": "NEXO_JETSON_YOLO",
        "version": 2,
        "ts": yolo_now,
        "pipeline": "direct_roadEncodeData",
        "source": "carrot_compressed_vipc",
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
