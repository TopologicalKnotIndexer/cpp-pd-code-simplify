from __future__ import annotations

import json
import sys
from pathlib import Path
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
            bruteforce_budget=int(request.get("bruteforce_budget", 200000)),
            timeout=int(request.get("timeout", -1)),
            verbose=bool(request.get("verbose", False)),
            show_step_pd=bool(request.get("show_step_pd", False)),
            reapr=bool(request.get("reapr", False)),
            reapr_retry_max=int(request.get("reapr_retry_max", 3)),
            known_crossingless_components=int(
                request.get("known_crossingless_components", 0)
            ),
            remove_crossings=request.get("remove_crossings") or [],
            log_file=request.get("log_file"),
        )
        output = json.dumps({"ok": True, "result": result}, separators=(",", ":"))
        protocol_output_path = request.get("protocol_output_path")
        if protocol_output_path:
            Path(str(protocol_output_path)).write_text(output, encoding="utf-8")
        else:
            print(output)
        return 0
    except KeyboardInterrupt:
        output = json.dumps(
            {"ok": False, "error": "interrupted by Ctrl+C"},
            separators=(",", ":"),
        )
        try:
            request
        except NameError:
            request = {}
        protocol_output_path = request.get("protocol_output_path")
        if protocol_output_path:
            Path(str(protocol_output_path)).write_text(output, encoding="utf-8")
        else:
            print(output)
        return 130
    except BaseException as exc:  # noqa: BLE001 - return errors through JSON.
        output = json.dumps(
            {"ok": False, "error": str(exc)},
            separators=(",", ":"),
        )
        try:
            request
        except NameError:
            request = {}
        protocol_output_path = request.get("protocol_output_path")
        if protocol_output_path:
            Path(str(protocol_output_path)).write_text(output, encoding="utf-8")
        else:
            print(output)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
