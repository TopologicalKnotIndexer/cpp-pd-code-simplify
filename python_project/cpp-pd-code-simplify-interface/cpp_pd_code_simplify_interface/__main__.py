import json

from .main import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted by Ctrl+C"}, indent=2))
        raise SystemExit(130)
