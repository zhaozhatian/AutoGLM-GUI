"""Generate a verification payload for the push ingest API.

Usage:
    python deploy/examples/make_push_payload.py
    python deploy/examples/make_push_payload.py --callback http://127.0.0.1:9000/hook
    python deploy/examples/make_push_payload.py --lead-minutes 0 --window-minutes 5

Writes verify-payload.json into the current working directory.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

DEVICE_A = "56fe42860624"
DEVICE_B = "75dcc86f0624"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", default=None, help="optional callback_url")
    parser.add_argument("--lead-minutes", type=int, default=2)
    parser.add_argument("--window-minutes", type=int, default=10)
    parser.add_argument("--batch-id", default=None)
    args = parser.parse_args()

    now = datetime.now().astimezone()
    batch_id = args.batch_id or f"verify-{now:%Y%m%d-%H%M%S}"
    window_start = now + timedelta(minutes=args.lead_minutes)
    window_end = window_start + timedelta(minutes=args.window_minutes)

    payload = {
        "external_batch_id": batch_id,
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": window_end.isoformat(timespec="seconds"),
        "items": [
            {
                "external_item_id": "g1",
                "device_serial": DEVICE_A,
                "group_name": "26.11.12小快灵测试一群",
                "message": "一群：验证消息第一行\n第二行：换行和标点需要保留，例如 12月1日、@陈丽。",
                "mention_all": True,
            },
            {
                "external_item_id": "g2",
                "device_serial": DEVICE_A,
                "group_name": "26.11.12小快灵测试二群",
                "message": "二群：验证消息第一行\n第二行：同设备两条消息应保持最小间隔。",
                "mention_all": True,
            },
            {
                "external_item_id": "g3",
                "device_serial": DEVICE_B,
                "group_name": "26.11.12小快灵测试三群",
                "message": "三群：验证消息\n第二行。",
                "mention_all": True,
            },
            {
                "external_item_id": "g4",
                "device_serial": DEVICE_B,
                "group_name": "26.11.12小快灵测试四群",
                "message": "四群：验证消息\n第二行。",
                "mention_all": True,
            },
        ],
    }
    if args.callback:
        payload["callback_url"] = args.callback

    target = Path.cwd() / "verify-payload.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"written: {target}")
    print(f"external_batch_id: {batch_id}")
    print(f"window: {payload['window_start']} -> {payload['window_end']}")


if __name__ == "__main__":
    main()
