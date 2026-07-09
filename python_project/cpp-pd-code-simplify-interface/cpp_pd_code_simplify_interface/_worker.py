from __future__ import annotations

import json
import sys
from typing import Any

from .main import _run_one_direct


def main() -> int:
    try:
        request: dict[str, Any] = json.loads(sys.stdin.read())
        result = _run_one_direct(
            str(request.get("pd_text", "")),
            max_paths=int(request.get("max_paths", -1)),
            ban_heuristic=bool(request.get("ban_heuristic", False)),
            reduction_round=int(request.get("reduction_round", -1)),
            max_thread=int(request.get("max_thread", -1)),
            timeout=int(request.get("timeout", -1)),
            verbose=bool(request.get("verbose", False)),
            known_crossingless_components=int(
                request.get("known_crossingless_components", 0)
            ),
            remove_crossings=request.get("remove_crossings") or [],
        )
        print(json.dumps({"ok": True, "result": result}, separators=(",", ":")))
        return 0
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"ok": False, "error": "interrupted by Ctrl+C"},
                separators=(",", ":"),
            )
        )
        return 130
    except BaseException as exc:  # noqa: BLE001 - return errors through JSON.
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                separators=(",", ":"),
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
