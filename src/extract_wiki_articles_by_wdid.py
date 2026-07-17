'''
Extract selected Wikipedia articles from WikiExtractor files by title mapping.
'''

import argparse
import html
import logging
import re
from collections.abc import Iterator
from pathlib import Path

LOG = logging.getLogger(__name__)

DOC_OPEN_RE = re.compile(r'^<doc\b[^>]*\btitle="(?P<title>[^"]*)"[^>]*>$')


def normalize_title(*, title: str) -> str:
    '''
    Normalize title text for matching.

    :param title: raw title string
    :return: normalized title
    '''
    return html.unescape(title).replace('_', ' ').strip()


def parse_doc_title(*, doc_line: str) -> str | None:
    '''
    Parse article title from a WikiExtractor doc opening line.

    :param doc_line: line starting with `<doc ...>`
    :return: extracted title or None if parsing fails
    '''
    match = DOC_OPEN_RE.match(doc_line.strip())
    if not match:
        return None
    return normalize_title(title=match.group('title'))


def load_title_mapping(*, mapping_path: Path) -> dict[str, list[str]]:
    '''
    Load title -> Wikidata IDs mapping from a TSV file.

    :param mapping_path: path to mapping file (`title<TAB>wdid`)
    :return: dictionary keyed by normalized title with WDID list
    '''
    title_to_wdids: dict[str, list[str]] = {}
    with mapping_path.open(encoding='utf-8') as in_file:
        for idx, raw_line in enumerate(in_file, start=1):
            line = raw_line.rstrip('\n')
            if not line:
                continue

            parts = line.split('\t')
            if len(parts) < 2:
                LOG.warning(
                    f'Skipping malformed mapping line, '
                    f'line_no={idx}, line={line!r}'
                )
                continue

            title = normalize_title(title=parts[0])
            wdid = parts[1].strip()
            if not title or not wdid:
                continue

            wdids = title_to_wdids.setdefault(title, [])
            if wdid in wdids:
                continue

            wdids.append(wdid)
    return title_to_wdids


def iter_wiki_files(*, wiki_input: Path) -> Iterator[Path]:
    '''
    Yield WikiExtractor input files.

    :param wiki_input: file or directory with extracted wiki data
    :return: iterator of input file paths
    '''
    if wiki_input.is_file():
        yield wiki_input
        return

    for path in sorted(wiki_input.rglob('*')):
        if path.is_file():
            yield path


def write_doc_to_wdids(
    *,
    doc_lines: list[str],
    wdids: list[str],
    out_dir: Path,
    wdid_counts: dict[str, int],
) -> int:
    '''
    Write one parsed article to all target WDIDs.

    :param doc_lines: article lines without `<doc>` wrappers
    :param wdids: output WDIDs for this article
    :param out_dir: output directory
    :param wdid_counts: per-WDID output counters
    :return: number of files written
    '''
    written = 0
    for wdid in wdids:
        article_no = wdid_counts.get(wdid, 0) + 1
        wdid_counts[wdid] = article_no
        out_path = out_dir / f'{wdid}_en_{article_no}.txt'
        with out_path.open('w', encoding='utf-8') as out_file:
            out_file.writelines(doc_lines)
        written += 1
    return written


def extract_articles(
    *,
    wiki_input: Path,
    title_to_wdids: dict[str, list[str]],
    out_dir: Path,
) -> tuple[int, int, int, int]:
    '''
    Stream WikiExtractor docs and write selected articles by mapped title.

    :param wiki_input: WikiExtractor file or directory
    :param title_to_wdids: mapping from normalized title to Wikidata IDs
    :param out_dir: destination directory
    :return: file count, scanned doc count, matched doc count,
        written file count
    '''
    out_dir.mkdir(parents=True, exist_ok=True)

    wdid_counts: dict[str, int] = {}
    files_count = 0
    docs_scanned = 0
    docs_matched = 0
    docs_written = 0

    for input_path in iter_wiki_files(wiki_input=wiki_input):
        files_count += 1
        LOG.info(f'Processing extracted file, path={input_path}')

        in_doc = False
        current_wdids: list[str] = []
        doc_lines: list[str] = []
        with input_path.open(encoding='utf-8') as in_file:
            for line in in_file:
                if line.startswith('<doc '):
                    in_doc = True
                    current_wdids = []
                    doc_lines = []
                    docs_scanned += 1

                    title = parse_doc_title(doc_line=line)
                    if not title:
                        LOG.warning(
                            f'Unable to parse doc title, file={input_path}, '
                            f'line={line.strip()!r}'
                        )
                        continue

                    wdids = title_to_wdids.get(title)
                    if not wdids:
                        continue

                    docs_matched += 1
                    current_wdids = wdids
                    continue

                if not in_doc:
                    continue

                if line.strip() == '</doc>':
                    in_doc = False
                    if current_wdids:
                        docs_written += write_doc_to_wdids(
                            doc_lines=doc_lines,
                            wdids=current_wdids,
                            out_dir=out_dir,
                            wdid_counts=wdid_counts,
                        )
                    current_wdids = []
                    doc_lines = []
                    continue

                if current_wdids:
                    doc_lines.append(line)

    return files_count, docs_scanned, docs_matched, docs_written


def main() -> None:
    '''
    Run CLI.
    '''
    argparser = argparse.ArgumentParser(
        description='Extract selected wiki articles by title-to-WDID mapping'
    )
    argparser.add_argument(
        '-w',
        '--wiki-input',
        required=True,
        type=Path,
        help='Path to a WikiExtractor file or folder (e.g. extracted-wiki)',
    )
    argparser.add_argument(
        '-m',
        '--mapping',
        required=True,
        type=Path,
        help='Path to title-to-WDID TSV mapping file (title<TAB>wdid)',
    )
    argparser.add_argument(
        '-o',
        '--output-dir',
        required=True,
        type=Path,
        help='Output folder for files named {wdid}_en_{n}.txt',
    )
    argparser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])

    args = argparser.parse_args()
    logging.basicConfig(level=args.log_level, format='%(levelname)s %(name)s: %(message)s')

    if not args.mapping.is_file():
        raise FileNotFoundError(f'Mapping file not found: {args.mapping}')
    if not args.wiki_input.exists():
        raise FileNotFoundError(f'Wiki input path not found: {args.wiki_input}')

    title_to_wdids = load_title_mapping(mapping_path=args.mapping)
    mappings_count = sum(len(wdids) for wdids in title_to_wdids.values())
    LOG.info(
        f'Loaded mapping entries, titles={len(title_to_wdids)}, '
        f'title_wdid_pairs={mappings_count}'
    )

    files_count, docs_scanned, docs_matched, docs_written = extract_articles(
        wiki_input=args.wiki_input,
        title_to_wdids=title_to_wdids,
        out_dir=args.output_dir,
    )
    LOG.info(
        'Extraction finished, '
        f'files={files_count}, '
        f'scanned_docs={docs_scanned}, '
        f'matched_docs={docs_matched}, '
        f'written_docs={docs_written}'
    )


if __name__ == '__main__':
    main()
