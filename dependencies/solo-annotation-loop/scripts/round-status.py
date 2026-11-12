#!/usr/bin/env python3
"""Read or advance a round state with strict transitions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

STATES = ["prepared", "project_profiled", "prompt_ready", "waiting_user", "round_located", "verified", "scored", "delivery_updated", "waiting_confirmation"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Show or advance round state")
    parser.add_argument("state", type=Path)
    parser.add_argument("--advance", choices=STATES)
    args = parser.parse_args()
    path = args.state.resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    current = data.get("status")
    if current not in STATES:
        parser.error(f"未知状态: {current}")
    if args.advance:
        expected = STATES[STATES.index(current) + 1] if current != STATES[-1] else None
        if args.advance != expected:
            parser.error(f"只能从 {current} 前进到 {expected or '无'}")
        data["status"] = args.advance
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
