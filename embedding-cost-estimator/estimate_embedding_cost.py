#!/usr/bin/env python3
"""Estimate OpenAI embedding cost for a folder of Markdown files.

Usage:
    pip install tiktoken
    python estimate_embedding_cost.py /path/to/markdown_folder

Counts tokens with OpenAI's own tokenizer (cl100k_base = the encoding used by
text-embedding-3-small/large), so the number matches what OpenAI will bill.
"""

import sys
from pathlib import Path

import tiktoken

# $ per 1M tokens (OpenAI embedding prices)
PRICES = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
}

MAX_TOKENS_PER_INPUT = 8191  # embedding model context limit; flag oversized files


def main(folder: str) -> None:
    root = Path(folder)
    if not root.is_dir():
        sys.exit(f"Not a directory: {folder}")

    enc = tiktoken.get_encoding("cl100k_base")
    files = sorted(root.rglob("*.md"))
    if not files:
        sys.exit(f"No .md files found under {folder}")

    total_tokens = 0
    oversized = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        n = len(enc.encode(text))
        total_tokens += n
        if n > MAX_TOKENS_PER_INPUT:
            oversized.append((f, n))

    print(f"Files:  {len(files)}")
    print(f"Tokens: {total_tokens:,}")
    print(f"        ({total_tokens / 1e6:.3f} million)\n")

    print("One-time embedding cost:")
    for model, price in PRICES.items():
        cost = total_tokens / 1e6 * price
        print(f"  {model:<26} ${cost:.4f}")

    if oversized:
        print(f"\n{len(oversized)} file(s) exceed {MAX_TOKENS_PER_INPUT} tokens "
              "(must be chunked before embedding):")
        for f, n in oversized[:10]:
            print(f"  {n:>7,}  {f.relative_to(root)}")
        if len(oversized) > 10:
            print(f"  ...and {len(oversized) - 10} more")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python estimate_embedding_cost.py /path/to/markdown_folder")
    main(sys.argv[1])
