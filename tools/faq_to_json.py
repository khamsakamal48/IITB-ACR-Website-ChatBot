#!/usr/bin/env python3
"""FAQ spreadsheet -> JSON body for the knowledge service's PUT /kb/faq.

Updates ONLY the bot's FAQ answers. Never rebuild + rsync the whole bundle to update the
FAQ once the website sync is live: that would revert every page the sync has refreshed.

  # laptop
  uv run --with openpyxl python -I tools/faq_to_json.py "Chatbot FAQ ACR.xlsx" > faq.json
  scp faq.json <user>@<server>:/opt/acr-chatbot/
  # server
  cd /opt/acr-chatbot && source .env
  curl -s -X PUT localhost:8080/kb/faq -H "Authorization: Bearer $KB_TOKEN" \
       -H 'Content-Type: application/json' --data @faq.json; echo     # {"categories":..,"answers":..}
  rm faq.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_okf import read_faq_xlsx  # noqa: E402  (same column matching as the bundle builder)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    xlsx = Path(sys.argv[1])
    rows = [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in read_faq_xlsx(xlsx)]
    answered = sum(1 for r in rows if r["question"].strip() and r["answer"].strip())
    print(f"{answered} Q&As read from {xlsx.name}", file=sys.stderr)
    json.dump({"rows": rows, "sheet_name": xlsx.name}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
