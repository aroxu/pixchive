"""Private JSON-lines bridge; credentials enter only through stdin.

No cookie persistence, process arguments, remote errors, or credential logging.
"""

import json
import logging
import sys

from app.adapters.pixiv.contracts import AdapterError
from app.adapters.pixiv.web import WebBridge, identifier


def main():
    logging.disable(logging.CRITICAL)
    bridge = None
    for line in sys.stdin:
        try:
            command = json.loads(line)
            if command["op"] == "open":
                bridge = WebBridge(command["cookie"], interval=command.get("interval", 0))
                result = True
            elif command["op"] == "validate" and bridge:
                result = bridge.validate()
            elif command["op"] == "listing" and bridge:
                result = bridge.bookmark_page(
                    command["account_id"], command["scope"], command["cursor"]
                )
            elif command["op"] == "artwork" and bridge:
                result = bridge.artwork(identifier(command["artwork_id"])).model_dump(mode="json")
            elif command["op"] == "verify" and bridge:
                result = bridge.verify(
                    command["account_id"], command["scope"], command["total"], command["first"]
                )
            else:
                raise AdapterError("contract")
            output = {"result": result}
        except AdapterError as exc:
            output = {"error": exc.code, "retry_after": exc.retry_after}
        except Exception:
            output = {"error": "contract"}
        sys.stdout.write(json.dumps(output, ensure_ascii=True) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
