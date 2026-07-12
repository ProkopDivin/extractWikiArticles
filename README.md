# Wikipedia Text Preparation (Master Thesis)

This repository prepares Wikipedia text data for an experimental AI master thesis workflow.

The required preparation flow is intentionally limited to **three steps**.

## Required Preparation Flow (Steps 1-3)

### 1) Download English Wikipedia dumps

Use:

```bash
./download-links.sh
```

This downloads the required dumps into `enwiki_dumps/` (including XML and SQL files).

### 2) Build title-to-Wikidata mapping

Use:

```bash
python src/fetch_enwiki_pages.py \
  -i wdId_ids.txt \
  -o wdid2wiki.txt
```

Output format is:

`page_title<TAB>origin_qid`

This output is consumed directly by the extraction step.

### 3) Extract text using WikiExtractor path only

Only the WikiExtractor-based path is used for text preparation.

#### 3a) Run WikiExtractor

```bash
cd wikiextractor

# extract.sh: INPUT PROCESSES TEMPLATES OUTPUT
./extract.sh \
  ../enwiki_dumps/enwiki-latest-pages-articles-multistream.xml.bz2 \
  8 \
  templates.jsonl \
  ../extracted-wiki
```

This creates sharded WikiExtractor output in `extracted-wiki/`.

#### 3b) Filter extracted documents by mapping

```bash
python src/extract_wiki_articles_by_wdid.py \
  -w extracted-wiki \
  -m wdid2wiki.txt \
  -o selected-articles
```

Result: one or more files per Wikidata ID in `selected-articles/` named `{wdid}_en_{n}.txt`.


### Graph scripts

These scripts are also kept for future graph experiments and validation:

- `src/wiki_graph_pipeline.py`
- `src/validate_wiki_graph_pipeline.py`

---

## Repository Layout (Current)

- `src/` - all project Python scripts
- `wikiextractor/` - vendored WikiExtractor
- `download-links.sh` - dump download helper
- `enwiki_dumps/` - downloaded Wikimedia dumps
- `extracted-wiki/` - raw WikiExtractor output
- `selected-articles/` - filtered article texts for thesis experiments
- `DECISIONS.md` - decision log

---

## Notes

- The previous alternative text-representation preparation path was removed from the codebase.
- The thesis preparation path now uses only WikiExtractor-based extraction.
- i planned to do experimentd with link graph ower wikipedia but this was canceled since it stopped being relevant 