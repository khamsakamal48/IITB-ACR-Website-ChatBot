# Embedding Cost Estimator

Estimate the one-time OpenAI embedding cost for a folder of Markdown files
before you run the real ingest. Uses OpenAI's own tokenizer (`cl100k_base`),
so the token count matches what OpenAI bills.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python estimate_embedding_cost.py /path/to/markdown_folder
```

Scans the folder and all subfolders for `.md` files, sums tokens, and prints
the cost for each embedding model.

### Example output

```
Files:  312
Tokens: 486,204
        (0.486 million)

One-time embedding cost:
  text-embedding-3-small     $0.0097
  text-embedding-3-large     $0.0632

2 file(s) exceed 8191 tokens (must be chunked before embedding):
   12,043  guides/long-policy.md
    9,880  about/history.md
```

## Prices

| Model | $/1M tokens | Dimensions |
|-------|-------------|------------|
| `text-embedding-3-small` | $0.02 | 1536 |
| `text-embedding-3-large` | $0.13 | 3072 |

Prices as of 2026. Update `PRICES` in the script if OpenAI changes them.

## Notes

- **Embedding cost is tiny** — usually cents. The real ingest cost is the
  Phase 2 LLM contextual-prefix generation (one LLM call per chunk), which is
  a separate and larger line item.
- Files over **8191 tokens** exceed the embedding input limit and must be
  chunked first. The script flags them; your Phase 1 chunker handles the split
  at ingest time (chunking doesn't change the total token count).
- Token count ≈ words × 1.33 ≈ chars ÷ 4, if you want a back-of-envelope check.
