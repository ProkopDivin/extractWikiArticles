# Wikipedia Text Preparation

This repository contains a reproducible Wikipedia text-preparation pipeline for this project:[https://github.com/ProkopDivin/entity-enhance-classification](https://github.com/ProkopDivin/entity-enhance-classification).

The **required thesis preparation flow** is intentionally limited to **three steps**.

## Required Preparation Flow (Steps 1-3)

### 1) Download English Wikipedia dumps

```bash
./download-links.sh
```

optionaly download just wikipedia article dump when prepering only wikidata entity text representations

```bash
nohup wget -P enwiki_dumps https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-pages-articles-multistream.xml.bz2 > download-dump.log 2>&1 &
```

This downloads required XML/SQL dumps into `enwiki_dumps/`.

### 2) Build title-to-Wikidata mapping

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/fetch_enwiki_pages.py \
  -i wdId_ids-sample.txt \
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

python -m wikiextractor.WikiExtractor ../enwiki_dumps/enwiki-latest-pages-articles-multistream.xml.bz2
```



#### 3c) Filter extracted documents by mapping

```bash
cd extractWikiArticles
python src/extract_wiki_articles_by_wdid.py \
  -w wikiextractor/text \
  -m wdid2wiki.txt \
  -o selected-articles
```

Result: one or more files per Wikidata ID in `selected-articles/` named `{wdid}_en_{n}.txt`, this can be used as input to make entity embeding in the [https://github.com/ProkopDivin/entity-enhance-classification](https://github.com/ProkopDivin/entity-enhance-classification) project, more info is in README.md of the project. 

## Reproducibility Checklist

- Use Python 3.10+ and the same dependency set across runs.
- Keep dump snapshot consistent for all experiments.
- Run from repository root so default relative paths resolve correctly.



## Optional Scripts - for future work (Not Part of Required Steps 1-3)



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

