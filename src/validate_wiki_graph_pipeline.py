'''
Validate outputs from wiki_graph_pipeline.py.
'''

import argparse
from pathlib import Path
from typing import TextIO

import duckdb
from geneea.core import logutil  # type: ignore[import-untyped]

LOG = logutil.getLogger(__package__, __file__)


def iter_seed_lines(*, in_file: TextIO) -> list[str]:
    '''
    Read non-empty, non-comment lines.

    :param in_file: input stream
    :return: list of lines
    '''
    out: list[str] = []
    for raw_line in in_file:
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        out.append(line)
    return out


def process(
    *,
    seed_file: Path,
    required_pages_path: Path,
    graph_index_path: Path,
    index_db_path: Path,
) -> None:
    '''
    Validate key output artifacts.

    :param seed_file: original seed title file
    :param required_pages_path: required pages output
    :param graph_index_path: per-seed graph manifest
    :param index_db_path: DuckDB index path
    '''
    if not required_pages_path.exists():
        raise FileNotFoundError(f'Missing required pages output: {required_pages_path}')
    if not graph_index_path.exists():
        raise FileNotFoundError(f'Missing graph index output: {graph_index_path}')
    if not index_db_path.exists():
        raise FileNotFoundError(f'Missing DuckDB index database: {index_db_path}')

    with seed_file.open(encoding='utf-8') as seed_in:
        seed_lines = iter_seed_lines(in_file=seed_in)
    with required_pages_path.open(encoding='utf-8') as req_in:
        required_lines = iter_seed_lines(in_file=req_in)
    with graph_index_path.open(encoding='utf-8') as graph_in:
        graph_lines = iter_seed_lines(in_file=graph_in)

    if len(required_lines) < len(seed_lines):
        raise ValueError(
            f'Required pages count too low, required={len(required_lines)}, seeds={len(seed_lines)}'
        )
    if len(graph_lines) <= 1:
        raise ValueError('Graph index appears empty')

    conn = duckdb.connect(str(index_db_path), read_only=True)
    try:
        page_count = conn.execute('SELECT COUNT(*) FROM pages').fetchone()
        redirect_count = conn.execute('SELECT COUNT(*) FROM redirects').fetchone()
        edge_count = conn.execute('SELECT COUNT(*) FROM edges').fetchone()
    finally:
        conn.close()

    page_rows = int(page_count[0]) if page_count else 0
    redirect_rows = int(redirect_count[0]) if redirect_count else 0
    edge_rows = int(edge_count[0]) if edge_count else 0
    if page_rows == 0 or edge_rows == 0:
        raise ValueError(
            f'Index rows invalid, pages={page_rows}, redirects={redirect_rows}, edges={edge_rows}'
        )

    LOG.info(
        f'Validation passed, seeds={len(seed_lines)}, required_pages={len(required_lines)}, '
        f'index_pages={page_rows}, index_redirects={redirect_rows}, index_edges={edge_rows}'
    )


def main() -> None:
    '''
    Run output validations.
    '''
    argparser = argparse.ArgumentParser(
        description='Validate dump-based Wikipedia graph outputs'
    )
    argparser.add_argument('--seed-file', type=Path, required=True, help='Seed title file used in pipeline')
    argparser.add_argument('--required-pages', type=Path, required=True, help='required_pages.txt path')
    argparser.add_argument('--graph-index', type=Path, required=True, help='graphs/index.tsv path')
    argparser.add_argument('--index-db', type=Path, required=True, help='DuckDB index path')

    logutil.addLogArguments(argparser)
    args = argparser.parse_args()
    logutil.configureFromArgs(args)

    process(
        seed_file=args.seed_file,
        required_pages_path=args.required_pages,
        graph_index_path=args.graph_index,
        index_db_path=args.index_db,
    )


if __name__ == '__main__':
    main()
