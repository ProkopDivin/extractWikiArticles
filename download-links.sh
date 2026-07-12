#!/bin/bash
OUT_DIR="enwiki_dumps"

URLS=(
  "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-page.sql.gz"
 "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-pagelinks.sql.gz"
  "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-redirect.sql.gz"
  "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-categorylinks.sql.gz"
  "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-externallinks.sql.gz"
  "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-pages-articles-multistream.xml.bz2"
)

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

for url in "${URLS[@]}"; do
  file_name="$(basename "$url")"

  echo "Downloading: $file_name"

  # -c resumes partial downloads
  # --show-progress displays progress
  wget -c --show-progress "$url"

  echo "Finished: $file_name"
  echo
done

echo "All downloads completed."
