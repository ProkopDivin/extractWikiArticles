# Wikipedia Text Preparation (Master Thesis)

This repository contains a reproducible Wikipedia text-preparation pipeline for this project:(entity-enhance-classification
Public
)[https://github.com/ProkopDivin/entity-enhance-classification].

The **required thesis preparation flow** is intentionally limited to **three steps**.

## Required Preparation Flow (Steps 1-3)

### 1) Download English Wikipedia dumps

```bash
./download-links.sh
```

This downloads required XML/SQL dumps into `enwiki_dumps/`.

### 2) Build title-to-Wikidata mapping

```bash
python src/fetch_enwiki_pages.py \
  -i wdId_ids.txt \
  -o wdid2wiki.txt
```

Output format:

`page_title<TAB>origin_qid`

### 3) Extract text 

Only the WikiExtractor path is used for thesis text preparation.

#### 3a) Get WikiExtractor (if not vendored in your clone)

```bash
# clone and pin for reproducibility
git clone https://github.com/attardi/wikiextractor.git
cd wikiextractor
```

If `wikiextractor/` already exists in your local repository, you can skip this step.

#### 3b) Run WikiExtractor

```bash
cd wikiextractor

# extract.sh: INPUT PROCESSES TEMPLATES OUTPUT
./extract.sh \
  ../enwiki_dumps/enwiki-latest-pages-articles-multistream.xml.bz2 \
  8 \
  templates.jsonl \
  ../extracted-wiki
```

#### 3c) Filter extracted documents by mapping

```bash
python src/extract_wiki_articles_by_wdid.py \
  -w extracted-wiki \
  -m wdid2wiki.txt \
  -o selected-articles
```

Result: one or more files per Wikidata ID in `selected-articles/` named `{wdid}_en_{n}.txt`.

## Reproducibility Checklist

- Use Python 3.10+ and the same dependency set across runs.
- Keep dump snapshot consistent for all experiments.
- Record commit hash and run command for every experiment batch.
- Run from repository root so default relative paths resolve correctly.

## Smoke Checks

After step 3:

```bash
ls selected-articles | wc -l
```

The count should be greater than zero.

## Optional Scripts (Not Part of Required Steps 1-3)

### Optional embeddings script

This script is intentionally kept for downstream experiments, but it is not part of mandatory thesis preparation:

```bash
python src/compute_article_embeddings.py \
  --in-dir selected-articles \
  --out-dir selected-article-embeddings
```

### Optional graph scripts

Graph experiments are currently out of thesis scope, but scripts are retained:
- `src/wiki_graph_pipeline.py`
- `src/validate_wiki_graph_pipeline.py`

Thous scripts are unfinished and not tested, keeping them for possible future work. 

## Repository Layout

- `src/` - project Python scripts
- `wikiextractor/` - vendored WikiExtractor
- `download-links.sh` - dump download helper
- `resources/` - link/performance configuration files
- `DECISIONS.md` - decision log

## Notes

- The previous alternative text-representation preparation path was removed from the codebase.
- The thesis preparation path now uses only WikiExtractor-based extraction.
 