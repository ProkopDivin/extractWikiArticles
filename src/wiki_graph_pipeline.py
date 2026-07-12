'''
Build dump-based Wikipedia graphs for seed page titles.
'''

import argparse
import bz2
import gzip
import re
import sys
import urllib.parse
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import duckdb
import mwparserfromhell
import mwxml
import wikitextparser as wtp
from geneea.core import logutil  # type: ignore[import-untyped]

LOG = logutil.getLogger(__package__, __file__)

ARTICLE_NS = 0
CATEGORY_NS = 14
SUPPORTED_NS = {ARTICLE_NS, CATEGORY_NS}
DBConnection = duckdb.DuckDBPyConnection
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class NodeKey:
    '''
    Graph node key.

    :param namespace: MediaWiki namespace
    :param title: normalized page title
    '''

    namespace: int
    title: str


def resolve_dump_path(
    *,
    dumps_dir: Path,
    base_name: str,
    compression_suffixes: Sequence[str],
) -> Path | None:
    '''
    Resolve dump file path for compressed/uncompressed variants.

    :param dumps_dir: dump directory
    :param base_name: base dump filename without compression suffix
    :param compression_suffixes: preferred compression suffixes
    :return: first existing path or None
    '''
    for suffix in compression_suffixes:
        candidate = dumps_dir / f'{base_name}{suffix}'
        if candidate.exists():
            return candidate
    return None


def open_text_maybe_compressed(*, path: Path) -> TextIO:
    '''
    Open text stream for compressed or plain file.

    :param path: file path
    :return: text stream
    '''
    if path.suffix == '.gz':
        return gzip.open(path, mode='rt', encoding='utf-8', errors='replace')
    if path.suffix == '.bz2':
        return bz2.open(path, mode='rt', encoding='utf-8', errors='replace')
    return path.open(mode='r', encoding='utf-8', errors='replace')


def open_binary_maybe_compressed(*, path: Path) -> TextIO:
    '''
    Open text stream for mwxml parser.

    :param path: file path
    :return: text stream
    '''
    if path.suffix == '.gz':
        return gzip.open(path, mode='rt', encoding='utf-8', errors='replace')
    if path.suffix == '.bz2':
        return bz2.open(path, mode='rt', encoding='utf-8', errors='replace')
    return path.open(mode='r', encoding='utf-8', errors='replace')


def parse_simple_yaml(*, path: Path) -> dict[str, str | int | bool]:
    '''
    Parse a flat YAML key-value file.

    :param path: YAML file path
    :return: parsed mapping
    '''
    out: dict[str, str | int | bool] = {}
    if not path.exists():
        return out

    with path.open(encoding='utf-8') as in_file:
        for raw_line in in_file:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' not in line:
                continue

            key, value = line.split(':', maxsplit=1)
            key = key.strip()
            value = value.strip()
            low = value.lower()
            if low in {'true', 'false'}:
                out[key] = low == 'true'
            elif value.isdigit():
                out[key] = int(value)
            else:
                out[key] = value
    return out


def normalize_title(*, title: str) -> str:
    '''
    Normalize MediaWiki title.

    :param title: raw title
    :return: normalized title
    '''
    value = urllib.parse.unquote(title).replace('_', ' ').strip()
    if '#' in value:
        value = value.split('#', maxsplit=1)[0]
    value = ' '.join(value.split())
    if not value:
        return value
    return value[0].upper() + value[1:]


def parse_seed_title(*, raw_title: str) -> str | None:
    '''
    Parse article title from seed input line.

    :param raw_title: raw page title
    :return: article title or None
    '''
    raw_title = raw_title.strip()
    if not raw_title:
        return None
    title = normalize_title(title=raw_title)
    if ':' in title:
        return None
    return title


def node_label(*, namespace: int, title: str) -> str:
    '''
    Build readable node label.

    :param namespace: namespace id
    :param title: normalized title
    :return: label for output
    '''
    if namespace == CATEGORY_NS:
        return f'Category:{title}'
    return title


def parse_link_target(*, raw_target: str) -> NodeKey | None:
    '''
    Parse a wikilink target to supported namespace.

    :param raw_target: target part of wikilink
    :return: parsed node key or None
    '''
    target = raw_target.split('|', maxsplit=1)[0].strip()
    if not target:
        return None
    if target.startswith(':'):
        target = target[1:]

    if ':' in target:
        prefix, rest = target.split(':', maxsplit=1)
        if prefix.lower() == 'category':
            title = normalize_title(title=rest)
            return NodeKey(namespace=CATEGORY_NS, title=title) if title else None
        return None

    title = normalize_title(title=target)
    if not title:
        return None
    return NodeKey(namespace=ARTICLE_NS, title=title)


def iter_seed_titles(*, in_file: TextIO) -> Iterator[str]:
    '''
    Stream seed article titles from title list.

    :param in_file: seed title input stream
    :return: article title iterator
    '''
    for raw_line in in_file:
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        title = parse_seed_title(raw_title=line)
        if not title:
            LOG.warning(f'Skipping invalid seed title, title={line}')
            continue
        yield title


def iter_insert_rows(*, sql_path: Path) -> Iterator[list[str | None]]:
    '''
    Stream rows from SQL dump INSERT statements.

    :param sql_path: SQL dump path (compressed or plain)
    :return: parsed row values
    '''
    seen_insert = False
    try:
        with open_text_maybe_compressed(path=sql_path) as in_file:
            for line in in_file:
                if not line.startswith('INSERT INTO'):
                    continue
                seen_insert = True
                pos = line.find('VALUES')
                if pos < 0:
                    continue
                payload = line[pos + len('VALUES'):].strip().rstrip(';\n')
                yield from parse_insert_payload(payload=payload)
    except gzip.BadGzipFile:
        if seen_insert:
            # Some mirrored dumps contain valid gzip content with trailing garbage.
            LOG.warning(
                f'Ignored trailing non-gzip bytes after SQL content, path={sql_path}'
            )
            return
        raise


def parse_insert_payload(*, payload: str) -> Iterator[list[str | None]]:
    '''
    Parse SQL values payload into rows.

    :param payload: text after VALUES keyword
    :return: row iterator
    '''
    idx = 0
    length = len(payload)
    while idx < length:
        if payload[idx] != '(':
            idx += 1
            continue

        idx += 1
        row: list[str | None] = []
        current: list[str] = []
        in_quote = False

        while idx < length:
            ch = payload[idx]
            if in_quote:
                if ch == '\\' and idx + 1 < length:
                    next_ch = payload[idx + 1]
                    if next_ch in {"\\", "'"}:
                        current.append(next_ch)
                        idx += 2
                        continue
                if ch == "'":
                    in_quote = False
                    idx += 1
                    continue
                current.append(ch)
                idx += 1
                continue

            if ch == "'":
                in_quote = True
                idx += 1
                continue

            if ch == ',':
                row.append(cast_sql_value(value=''.join(current).strip()))
                current = []
                idx += 1
                continue

            if ch == ')':
                row.append(cast_sql_value(value=''.join(current).strip()))
                idx += 1
                break

            current.append(ch)
            idx += 1

        yield row
        while idx < length and payload[idx] in {',', ' ', '\n', '\r', '\t'}:
            idx += 1


def cast_sql_value(*, value: str) -> str | None:
    '''
    Convert SQL literal to Python value.

    :param value: token string
    :return: string or None
    '''
    if value.upper() == 'NULL':
        return None
    return value


def configure_duckdb(*, conn: DBConnection, perf_cfg: Mapping[str, str | int | bool]) -> None:
    '''
    Apply DuckDB performance settings.

    :param conn: duckdb connection
    :param perf_cfg: perf config mapping
    '''
    db_threads = int(perf_cfg.get('duckdb_threads', 4))
    memory_limit = str(perf_cfg.get('duckdb_memory_limit', '4GB'))
    conn.execute(f'PRAGMA threads={db_threads}')
    conn.execute(f"PRAGMA memory_limit='{memory_limit}'")


def create_index_schema(*, conn: DBConnection) -> None:
    '''
    Create index database schema.

    :param conn: duckdb connection
    '''
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS pages (
            page_id INTEGER PRIMARY KEY,
            namespace INTEGER NOT NULL,
            title TEXT NOT NULL,
            is_redirect INTEGER NOT NULL
        );
        '''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS pages_ns_title_idx ON pages(namespace, title)')
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS redirects (
            namespace INTEGER NOT NULL,
            source_title TEXT NOT NULL,
            target_namespace INTEGER NOT NULL,
            target_title TEXT NOT NULL,
            PRIMARY KEY(namespace, source_title)
        );
        '''
    )
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS edges (
            src_id INTEGER NOT NULL,
            dst_namespace INTEGER NOT NULL,
            dst_title TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            PRIMARY KEY(src_id, dst_namespace, dst_title, edge_type)
        );
        '''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS edges_src_idx ON edges(src_id)')
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        '''
    )
    conn.commit()


def load_pages(*, conn: DBConnection, page_sql_path: Path, perf_cfg: Mapping[str, str | int | bool]) -> None:
    '''
    Load pages from page.sql dump.

    :param conn: duckdb connection
    :param page_sql_path: page SQL dump path
    :param perf_cfg: perf config mapping
    '''
    insert_batch_size = int(perf_cfg.get('insert_batch_size', 50000))
    commit_every = int(perf_cfg.get('commit_every', 5))
    batch: list[tuple[int, int, str, int]] = []
    commits = 0
    total = 0

    for row in iter_insert_rows(sql_path=page_sql_path):
        if len(row) < 5 or row[0] is None or row[1] is None or row[2] is None or row[4] is None:
            continue
        page_id = int(row[0])
        namespace = int(row[1])
        if namespace not in SUPPORTED_NS:
            continue
        title = normalize_title(title=str(row[2]))
        if not title:
            continue
        is_redirect = int(row[4])
        batch.append((page_id, namespace, title, is_redirect))
        total += 1

        if len(batch) >= insert_batch_size:
            conn.executemany(
                'INSERT OR REPLACE INTO pages(page_id, namespace, title, is_redirect) VALUES(?, ?, ?, ?)',
                batch,
            )
            batch = []
            commits += 1
            if commits % commit_every == 0:
                conn.commit()
                LOG.info(f'Loaded pages, rows={total}')

    if batch:
        conn.executemany(
            'INSERT OR REPLACE INTO pages(page_id, namespace, title, is_redirect) VALUES(?, ?, ?, ?)',
            batch,
        )
    conn.commit()
    LOG.info(f'Completed page import, rows={total}')


def load_redirects(
    *,
    conn: DBConnection,
    redirect_sql_path: Path,
    perf_cfg: Mapping[str, str | int | bool],
) -> None:
    '''
    Load redirects from redirect.sql dump.

    :param conn: duckdb connection
    :param redirect_sql_path: redirect SQL dump path
    :param perf_cfg: perf config mapping
    '''
    conn.execute('DROP TABLE IF EXISTS redirect_raw')
    conn.execute(
        '''
        CREATE TABLE redirect_raw (
            page_id INTEGER NOT NULL,
            source_namespace INTEGER NOT NULL,
            target_namespace INTEGER NOT NULL,
            target_title TEXT NOT NULL
        )
        '''
    )
    conn.commit()

    insert_batch_size = int(perf_cfg.get('insert_batch_size', 50000))
    batch: list[tuple[int, int, int, str]] = []
    total = 0

    for row in iter_insert_rows(sql_path=redirect_sql_path):
        if len(row) < 3 or row[0] is None or row[1] is None or row[2] is None:
            continue
        page_id = int(row[0])
        target_namespace = int(row[1])
        if target_namespace not in SUPPORTED_NS:
            continue
        target_title = normalize_title(title=str(row[2]))
        if not target_title:
            continue

        batch.append((page_id, target_namespace, target_namespace, target_title))
        total += 1
        if len(batch) >= insert_batch_size:
            conn.executemany(
                'INSERT INTO redirect_raw(page_id, source_namespace, target_namespace, target_title) '
                'VALUES(?, ?, ?, ?)',
                batch,
            )
            batch = []

    if batch:
        conn.executemany(
            'INSERT INTO redirect_raw(page_id, source_namespace, target_namespace, target_title) VALUES(?, ?, ?, ?)',
            batch,
        )
    conn.commit()

    conn.execute('DELETE FROM redirects')
    conn.execute(
        '''
        INSERT OR REPLACE INTO redirects(namespace, source_title, target_namespace, target_title)
        SELECT p.namespace, p.title, r.target_namespace, r.target_title
        FROM redirect_raw r
        JOIN pages p ON p.page_id = r.page_id
        WHERE p.namespace = r.source_namespace
        '''
    )
    conn.commit()
    conn.execute('DROP TABLE redirect_raw')
    conn.commit()
    LOG.info(f'Completed redirect import, rows={total}')


def load_pagelinks(
    *,
    conn: DBConnection,
    pagelinks_sql_path: Path,
    perf_cfg: Mapping[str, str | int | bool],
) -> None:
    '''
    Load internal pagelinks as edges.

    :param conn: duckdb connection
    :param pagelinks_sql_path: pagelinks SQL dump path
    :param perf_cfg: perf config mapping
    '''
    insert_batch_size = int(perf_cfg.get('insert_batch_size', 50000))
    batch: list[tuple[int, int, str, str]] = []
    total = 0

    for row in iter_insert_rows(sql_path=pagelinks_sql_path):
        if len(row) < 3 or row[0] is None or row[1] is None or row[2] is None:
            continue
        src_id = int(row[0])
        dst_namespace = int(row[1])
        if dst_namespace != ARTICLE_NS:
            continue
        dst_title = normalize_title(title=str(row[2]))
        if not dst_title:
            continue

        batch.append((src_id, dst_namespace, dst_title, 'internal'))
        total += 1
        if len(batch) >= insert_batch_size:
            conn.executemany(
                'INSERT OR IGNORE INTO edges(src_id, dst_namespace, dst_title, edge_type) VALUES(?, ?, ?, ?)',
                batch,
            )
            batch = []

    if batch:
        conn.executemany(
            'INSERT OR IGNORE INTO edges(src_id, dst_namespace, dst_title, edge_type) VALUES(?, ?, ?, ?)',
            batch,
        )
    conn.commit()
    LOG.info(f'Completed pagelinks import, rows={total}')


def load_categorylinks(
    *,
    conn: DBConnection,
    categorylinks_sql_path: Path,
    perf_cfg: Mapping[str, str | int | bool],
) -> None:
    '''
    Load category links as edges.

    :param conn: duckdb connection
    :param categorylinks_sql_path: categorylinks SQL dump path
    :param perf_cfg: perf config mapping
    '''
    insert_batch_size = int(perf_cfg.get('insert_batch_size', 50000))
    batch: list[tuple[int, int, str, str]] = []
    total = 0

    for row in iter_insert_rows(sql_path=categorylinks_sql_path):
        if len(row) < 2 or row[0] is None or row[1] is None:
            continue
        src_id = int(row[0])
        dst_title = normalize_title(title=str(row[1]))
        if not dst_title:
            continue
        batch.append((src_id, CATEGORY_NS, dst_title, 'category'))
        total += 1
        if len(batch) >= insert_batch_size:
            conn.executemany(
                'INSERT OR IGNORE INTO edges(src_id, dst_namespace, dst_title, edge_type) VALUES(?, ?, ?, ?)',
                batch,
            )
            batch = []

    if batch:
        conn.executemany(
            'INSERT OR IGNORE INTO edges(src_id, dst_namespace, dst_title, edge_type) VALUES(?, ?, ?, ?)',
            batch,
        )
    conn.commit()
    LOG.info(f'Completed categorylinks import, rows={total}')


def iter_xml_pages(*, xml_path: Path) -> Iterator[tuple[int, str, str]]:
    '''
    Stream page text from XML dump.

    :param xml_path: XML dump path
    :return: iterator of (namespace, title, text)
    '''
    with open_binary_maybe_compressed(path=xml_path) as in_file:
        dump = mwxml.Dump.from_file(in_file)
        for page in dump:
            if page.namespace != ARTICLE_NS:
                continue
            if page.title is None:
                continue
            title = normalize_title(title=page.title)
            if not title:
                continue
            text_value = ''
            for revision in page:
                if revision.text:
                    text_value = revision.text
            yield ARTICLE_NS, title, text_value


def extract_wikilinks(*, text: str) -> Iterator[NodeKey]:
    '''
    Extract supported wikilink targets from text.

    :param text: source markup
    :return: parsed targets
    '''
    wikicode = mwparserfromhell.parse(text)
    for link in wikicode.filter_wikilinks():
        target = parse_link_target(raw_target=str(link.title))
        if target is not None:
            yield target


def extract_see_also_links(*, text: str) -> Iterator[NodeKey]:
    '''
    Extract links from the See also section.

    :param text: page markup
    :return: parsed targets
    '''
    parsed = wtp.parse(text)
    for section in parsed.sections:
        title = (section.title or '').strip().lower()
        if title == 'see also':
            yield from extract_wikilinks(text=section.string)


def extract_ref_links(*, text: str) -> Iterator[NodeKey]:
    '''
    Extract links from ref blocks.

    :param text: page markup
    :return: parsed targets
    '''
    wikicode = mwparserfromhell.parse(text)
    for tag in wikicode.filter_tags():
        if str(tag.tag).lower() == 'ref':
            yield from extract_wikilinks(text=str(tag.contents))


def load_xml_edges(
    *,
    conn: DBConnection,
    xml_path: Path,
    perf_cfg: Mapping[str, str | int | bool],
    enable_see_also: bool,
    enable_ref_wikilink: bool,
) -> None:
    '''
    Load see-also and ref edges from article text.

    :param conn: duckdb connection
    :param xml_path: xml dump path
    :param perf_cfg: perf config mapping
    :param enable_see_also: include see_also edges
    :param enable_ref_wikilink: include ref_wikilink edges
    '''
    if not enable_see_also and not enable_ref_wikilink:
        return

    insert_batch_size = int(perf_cfg.get('insert_batch_size', 50000))
    queue_log_every = int(perf_cfg.get('queue_log_every', 100000))
    batch: list[tuple[int, int, str, str]] = []
    processed_pages = 0
    inserted = 0

    for _namespace, src_title, text in iter_xml_pages(xml_path=xml_path):
        src_id = get_page_id(conn=conn, namespace=ARTICLE_NS, title=src_title)
        if src_id is None:
            continue

        if enable_see_also:
            for target in extract_see_also_links(text=text):
                batch.append((src_id, target.namespace, target.title, 'see_also'))
                inserted += 1
        if enable_ref_wikilink:
            for target in extract_ref_links(text=text):
                batch.append((src_id, target.namespace, target.title, 'ref_wikilink'))
                inserted += 1

        if len(batch) >= insert_batch_size:
            conn.executemany(
                'INSERT OR IGNORE INTO edges(src_id, dst_namespace, dst_title, edge_type) VALUES(?, ?, ?, ?)',
                batch,
            )
            batch = []

        processed_pages += 1
        if processed_pages % queue_log_every == 0:
            conn.commit()
            LOG.info(f'Parsed XML pages, pages={processed_pages}, queued_edges={inserted}')

    if batch:
        conn.executemany(
            'INSERT OR IGNORE INTO edges(src_id, dst_namespace, dst_title, edge_type) VALUES(?, ?, ?, ?)',
            batch,
        )
    conn.commit()
    LOG.info(f'Completed XML link extraction, pages={processed_pages}, queued_edges={inserted}')


def get_page_id(*, conn: DBConnection, namespace: int, title: str) -> int | None:
    '''
    Resolve page ID from namespace and title.

    :param conn: duckdb connection
    :param namespace: namespace id
    :param title: normalized title
    :return: page id or None
    '''
    row = conn.execute(
        'SELECT page_id FROM pages WHERE namespace = ? AND title = ?',
        (namespace, title),
    ).fetchone()
    return int(row[0]) if row else None


def resolve_redirect(
    *,
    conn: DBConnection,
    node: NodeKey,
    max_hops: int = 20,
) -> tuple[NodeKey, bool]:
    '''
    Resolve node to canonical non-redirect target.

    :param conn: duckdb connection
    :param node: source node
    :param max_hops: max redirect hops
    :return: (canonical node, redirect_used)
    '''
    current = node
    visited: set[NodeKey] = set()
    redirect_used = False

    for _ in range(max_hops):
        if current in visited:
            LOG.warning(f'Redirect cycle detected, node={node_label(namespace=current.namespace, title=current.title)}')
            break
        visited.add(current)

        row = conn.execute(
            'SELECT target_namespace, target_title FROM redirects WHERE namespace = ? AND source_title = ?',
            (current.namespace, current.title),
        ).fetchone()
        if not row:
            break

        redirect_used = True
        current = NodeKey(namespace=int(row[0]), title=str(row[1]))

    return current, redirect_used


def iter_outgoing_edges(
    *,
    conn: DBConnection,
    src_id: int,
    enabled_edge_types: set[str],
) -> Iterator[tuple[NodeKey, str]]:
    '''
    Stream outgoing edges for source page.

    :param conn: duckdb connection
    :param src_id: source page id
    :param enabled_edge_types: enabled edge type names
    :return: iterator of (target, edge_type)
    '''
    placeholders = ', '.join('?' for _ in enabled_edge_types)
    query = (
        f'SELECT dst_namespace, dst_title, edge_type FROM edges '
        f'WHERE src_id = ? AND edge_type IN ({placeholders})'
    )
    params: list[object] = [src_id]
    params.extend(sorted(enabled_edge_types))
    rows = conn.execute(query, params).fetchall()
    for dst_namespace, dst_title, edge_type in rows:
        target = NodeKey(namespace=int(dst_namespace), title=str(dst_title))
        yield target, str(edge_type)


def reset_seed_graph_tables(*, conn: DBConnection) -> None:
    '''
    Reset temp tables for one seed traversal.

    :param conn: duckdb connection
    '''
    conn.execute('DROP TABLE IF EXISTS seed_nodes')
    conn.execute('DROP TABLE IF EXISTS seed_edges')
    conn.execute(
        '''
        CREATE TEMP TABLE seed_nodes (
            node_id BIGINT PRIMARY KEY,
            namespace INTEGER NOT NULL,
            title TEXT NOT NULL,
            depth INTEGER NOT NULL,
            enqueued INTEGER NOT NULL DEFAULT 0,
            UNIQUE(namespace, title)
        )
        '''
    )
    conn.execute(
        '''
        CREATE TEMP TABLE seed_edges (
            src_node_id INTEGER NOT NULL,
            dst_node_id INTEGER NOT NULL,
            edge_type TEXT NOT NULL,
            depth INTEGER NOT NULL,
            UNIQUE(src_node_id, dst_node_id, edge_type)
        )
        '''
    )
    conn.commit()


def get_or_create_seed_node(
    *,
    conn: DBConnection,
    node: NodeKey,
    depth: int,
) -> int:
    '''
    Upsert seed node and return local node id.

    :param conn: duckdb connection
    :param node: node key
    :param depth: traversal depth for node
    :return: local node id
    '''
    row = conn.execute(
        'SELECT node_id FROM seed_nodes WHERE namespace = ? AND title = ?',
        (node.namespace, node.title),
    ).fetchone()
    if row:
        node_id = int(row[0])
        conn.execute(
            'UPDATE seed_nodes SET depth = LEAST(depth, ?) WHERE node_id = ?',
            (depth, node_id),
        )
        return node_id

    next_row = conn.execute('SELECT COALESCE(MAX(node_id), 0) + 1 FROM seed_nodes').fetchone()
    if not next_row:
        raise RuntimeError('Failed to generate seed node id')
    node_id = int(next_row[0])
    conn.execute(
        'INSERT INTO seed_nodes(node_id, namespace, title, depth, enqueued) VALUES(?, ?, ?, ?, 0)',
        (node_id, node.namespace, node.title, depth),
    )
    return node_id


def mark_node_enqueued(*, conn: DBConnection, node_id: int) -> bool:
    '''
    Mark node as queued once.

    :param conn: duckdb connection
    :param node_id: local node id
    :return: true if node was newly marked
    '''
    row = conn.execute(
        'SELECT enqueued FROM seed_nodes WHERE node_id = ?',
        (node_id,),
    ).fetchone()
    if not row:
        return False
    already = int(row[0]) == 1
    if already:
        return False
    conn.execute(
        'UPDATE seed_nodes SET enqueued = 1 WHERE node_id = ?',
        (node_id,),
    )
    return True


def write_graphml_from_db(
    *,
    conn: DBConnection,
    graph_path: Path,
    compress: bool,
) -> tuple[int, int]:
    '''
    Write GraphML from temp seed tables.

    :param conn: duckdb connection
    :param graph_path: output graph path
    :param compress: write gzip GraphML
    :return: (node_count, edge_count)
    '''
    target_path = graph_path.with_suffix(graph_path.suffix + '.gz') if compress else graph_path
    if compress:
        out_file = gzip.open(target_path, mode='wt', encoding='utf-8')
    else:
        out_file = open(target_path, mode='w', encoding='utf-8')

    node_row = conn.execute('SELECT COUNT(*) FROM seed_nodes').fetchone()
    edge_row = conn.execute('SELECT COUNT(*) FROM seed_edges').fetchone()
    node_count = int(node_row[0]) if node_row else 0
    edge_count = int(edge_row[0]) if edge_row else 0

    with out_file:
        out_file.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out_file.write('<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n')
        out_file.write('  <key id="k_label" for="node" attr.name="label" attr.type="string"/>\n')
        out_file.write('  <key id="k_ns" for="node" attr.name="namespace" attr.type="int"/>\n')
        out_file.write('  <key id="k_depth" for="node" attr.name="depth" attr.type="int"/>\n')
        out_file.write('  <key id="k_type" for="edge" attr.name="edge_type" attr.type="string"/>\n')
        out_file.write('  <key id="k_step" for="edge" attr.name="step_depth" attr.type="int"/>\n')
        out_file.write('  <graph id="G" edgedefault="directed">\n')

        node_rows = conn.execute(
            'SELECT node_id, namespace, title, depth FROM seed_nodes ORDER BY node_id'
        ).fetchall()
        for node_id, namespace, title, depth in node_rows:
            label = escape_xml(value=node_label(namespace=int(namespace), title=str(title)))
            out_file.write(f'    <node id="n{node_id}">\n')
            out_file.write(f'      <data key="k_label">{label}</data>\n')
            out_file.write(f'      <data key="k_ns">{namespace}</data>\n')
            out_file.write(f'      <data key="k_depth">{depth}</data>\n')
            out_file.write('    </node>\n')

        edge_rows = conn.execute(
            '''
            SELECT src_node_id, dst_node_id, edge_type, depth
            FROM seed_edges
            ORDER BY src_node_id, dst_node_id, edge_type
            '''
        ).fetchall()
        for edge_id, (src_id, dst_id, edge_type, depth) in enumerate(edge_rows):
            edge_value = escape_xml(value=str(edge_type))
            out_file.write(f'    <edge id="e{edge_id}" source="n{src_id}" target="n{dst_id}">\n')
            out_file.write(f'      <data key="k_type">{edge_value}</data>\n')
            out_file.write(f'      <data key="k_step">{depth}</data>\n')
            out_file.write('    </edge>\n')

        out_file.write('  </graph>\n')
        out_file.write('</graphml>\n')

    return node_count, edge_count


def escape_xml(*, value: str) -> str:
    '''
    Escape XML special characters.

    :param value: raw string
    :return: escaped string
    '''
    return (
        value.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&apos;')
    )


def traverse_seed(
    *,
    conn: DBConnection,
    seed_title: str,
    max_depth: int,
    enabled_edge_types: set[str],
) -> tuple[int, int]:
    '''
    Traverse one seed and build graph data.

    :param conn: duckdb connection
    :param seed_title: seed article title
    :param max_depth: traversal depth
    :param enabled_edge_types: enabled edge type names
    :return: (node_count, edge_count)
    '''
    reset_seed_graph_tables(conn=conn)

    start_node = NodeKey(namespace=ARTICLE_NS, title=seed_title)
    canonical_start, _redirected = resolve_redirect(conn=conn, node=start_node)
    if get_page_id(conn=conn, namespace=canonical_start.namespace, title=canonical_start.title) is None:
        LOG.warning(f'Seed not found in index, seed={seed_title}')
        return 0, 0

    start_id = get_or_create_seed_node(
        conn=conn,
        node=canonical_start,
        depth=0,
    )
    mark_node_enqueued(conn=conn, node_id=start_id)
    queue: deque[tuple[int, NodeKey, int]] = deque([(start_id, canonical_start, 0)])

    while queue:
        src_local_id, src_node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        src_id = get_page_id(conn=conn, namespace=src_node.namespace, title=src_node.title)
        if src_id is None:
            continue

        for target, edge_type in iter_outgoing_edges(
            conn=conn,
            src_id=src_id,
            enabled_edge_types=enabled_edge_types,
        ):
            canonical_target, _used_redirect = resolve_redirect(conn=conn, node=target)
            if get_page_id(
                conn=conn,
                namespace=canonical_target.namespace,
                title=canonical_target.title,
            ) is None:
                continue

            dst_local_id = get_or_create_seed_node(
                conn=conn,
                node=canonical_target,
                depth=depth + 1,
            )
            conn.execute(
                '''
                INSERT OR IGNORE INTO seed_edges(src_node_id, dst_node_id, edge_type, depth)
                VALUES(?, ?, ?, ?)
                ''',
                (src_local_id, dst_local_id, edge_type, depth + 1),
            )

            if depth + 1 <= max_depth and mark_node_enqueued(
                conn=conn,
                node_id=dst_local_id,
            ):
                queue.append((dst_local_id, canonical_target, depth + 1))

    conn.commit()
    node_row = conn.execute('SELECT COUNT(*) FROM seed_nodes').fetchone()
    edge_row = conn.execute('SELECT COUNT(*) FROM seed_edges').fetchone()
    node_count = int(node_row[0]) if node_row else 0
    edge_count = int(edge_row[0]) if edge_row else 0
    return node_count, edge_count


def build_index(
    *,
    index_db_path: Path,
    dumps_dir: Path,
    perf_cfg: Mapping[str, str | int | bool],
    enable_see_also: bool,
    enable_ref_wikilink: bool,
) -> None:
    '''
    Build DuckDB index from dump files.

    :param index_db_path: DuckDB output path
    :param dumps_dir: dump directory path
    :param perf_cfg: perf config mapping
    :param enable_see_also: include see-also extraction
    :param enable_ref_wikilink: include ref extraction
    '''
    page_sql = resolve_dump_path(
        dumps_dir=dumps_dir,
        base_name='enwiki-latest-page.sql',
        compression_suffixes=['', '.gz'],
    )
    redirect_sql = resolve_dump_path(
        dumps_dir=dumps_dir,
        base_name='enwiki-latest-redirect.sql',
        compression_suffixes=['', '.gz'],
    )
    pagelinks_sql = resolve_dump_path(
        dumps_dir=dumps_dir,
        base_name='enwiki-latest-pagelinks.sql',
        compression_suffixes=['', '.gz'],
    )
    categorylinks_sql = resolve_dump_path(
        dumps_dir=dumps_dir,
        base_name='enwiki-latest-categorylinks.sql',
        compression_suffixes=['', '.gz'],
    )
    xml_dump = resolve_dump_path(
        dumps_dir=dumps_dir,
        base_name='enwiki-latest-pages-articles-multistream.xml',
        compression_suffixes=['', '.bz2', '.gz'],
    )

    if not page_sql or not redirect_sql or not pagelinks_sql:
        raise FileNotFoundError(
            'Missing required dumps: page.sql(.gz), redirect.sql(.gz), pagelinks.sql(.gz)'
        )

    index_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(index_db_path))
    try:
        configure_duckdb(conn=conn, perf_cfg=perf_cfg)
        create_index_schema(conn=conn)
        conn.execute('DELETE FROM pages')
        conn.execute('DELETE FROM redirects')
        conn.execute('DELETE FROM edges')
        conn.commit()

        LOG.info('Loading page index')
        load_pages(conn=conn, page_sql_path=page_sql, perf_cfg=perf_cfg)
        LOG.info('Loading redirects')
        load_redirects(conn=conn, redirect_sql_path=redirect_sql, perf_cfg=perf_cfg)
        LOG.info('Loading pagelinks')
        load_pagelinks(conn=conn, pagelinks_sql_path=pagelinks_sql, perf_cfg=perf_cfg)

        if categorylinks_sql:
            LOG.info('Loading category links')
            load_categorylinks(
                conn=conn,
                categorylinks_sql_path=categorylinks_sql,
                perf_cfg=perf_cfg,
            )
        else:
            LOG.warning('Category links dump not found; category edges disabled')

        if xml_dump:
            LOG.info('Loading XML edges')
            load_xml_edges(
                conn=conn,
                xml_path=xml_dump,
                perf_cfg=perf_cfg,
                enable_see_also=enable_see_also,
                enable_ref_wikilink=enable_ref_wikilink,
            )
        else:
            LOG.warning('XML dump not found; see_also and ref_wikilink edges disabled')

        conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)', ('index_ready', '1'))
        conn.commit()
    finally:
        conn.close()


def process(
    *,
    in_file: TextIO,
    out_file: TextIO,
    depth: int,
    dumps_dir: Path,
    index_db_path: Path,
    output_dir: Path,
    link_cfg_path: Path,
    perf_cfg_path: Path,
    rebuild_index: bool,
) -> None:
    '''
    Run full graph extraction pipeline.

    :param in_file: seed title input stream
    :param out_file: required pages output stream
    :param depth: traversal depth
    :param dumps_dir: dump directory
    :param index_db_path: duckdb index path
    :param output_dir: output directory
    :param link_cfg_path: link config path
    :param perf_cfg_path: perf config path
    :param rebuild_index: rebuild index if true
    '''
    link_cfg = parse_simple_yaml(path=link_cfg_path)
    perf_cfg = parse_simple_yaml(path=perf_cfg_path)
    enabled_edge_types = {
        edge_name
        for edge_name in {'internal', 'see_also', 'category', 'ref_wikilink'}
        if bool(link_cfg.get(edge_name, True))
    }
    if not enabled_edge_types:
        raise ValueError('No edge types enabled in link config')

    if rebuild_index or not index_db_path.exists():
        build_index(
            index_db_path=index_db_path,
            dumps_dir=dumps_dir,
            perf_cfg=perf_cfg,
            enable_see_also='see_also' in enabled_edge_types,
            enable_ref_wikilink='ref_wikilink' in enabled_edge_types,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    graph_dir = output_dir / 'graphs'
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph_index_path = graph_dir / 'index.tsv'

    compress_graphml = bool(perf_cfg.get('compress_graphml', False))
    seed_titles = list(dict.fromkeys(iter_seed_titles(in_file=in_file)))
    if not seed_titles:
        raise ValueError('No valid seed titles found')

    conn = duckdb.connect(str(index_db_path))
    try:
        conn.execute('DROP TABLE IF EXISTS required_nodes')
        conn.execute(
            '''
            CREATE TABLE required_nodes (
                namespace INTEGER NOT NULL,
                title TEXT NOT NULL,
                UNIQUE(namespace, title)
            )
            '''
        )
        conn.commit()

        with graph_index_path.open('w', encoding='utf-8') as graph_index:
            graph_index.write('seed_title\tgraph_path\tnodes\tedges\tmax_depth\n')
            for idx, seed_title in enumerate(seed_titles, start=1):
                node_count, edge_count = traverse_seed(
                    conn=conn,
                    seed_title=seed_title,
                    max_depth=depth,
                    enabled_edge_types=enabled_edge_types,
                )
                if node_count == 0:
                    continue

                file_stem = re.sub(r'[^A-Za-z0-9._-]+', '_', seed_title)
                graph_path = graph_dir / f'{file_stem}.graphml'
                node_count, edge_count = write_graphml_from_db(
                    conn=conn,
                    graph_path=graph_path,
                    compress=compress_graphml,
                )
                conn.execute(
                    '''
                    INSERT OR IGNORE INTO required_nodes(namespace, title)
                    SELECT namespace, title FROM seed_nodes
                    '''
                )
                conn.commit()

                graph_file = graph_path.name + ('.gz' if compress_graphml else '')
                graph_index.write(
                    f'{seed_title}\t{graph_file}\t{node_count}\t{edge_count}\t{depth}\n'
                )
                LOG.info(
                    f'Completed seed graph, idx={idx}, seed={seed_title}, '
                    f'nodes={node_count}, edges={edge_count}'
                )
        required_row = conn.execute('SELECT COUNT(*) FROM required_nodes').fetchone()
        required_count = int(required_row[0]) if required_row else 0

        req_rows = conn.execute(
            'SELECT namespace, title FROM required_nodes ORDER BY namespace, title'
        ).fetchall()
        for namespace, title in req_rows:
            node_path = node_label(namespace=int(namespace), title=str(title)).replace(' ', '_')
            quoted_path = urllib.parse.quote(node_path)
            out_file.write(f'https://en.wikipedia.org/wiki/{quoted_path}\n')
    finally:
        conn.close()

    LOG.info(
        f'Completed graph extraction, seeds={len(seed_titles)}, '
        f'required_pages={required_count}, output_dir={output_dir}'
    )


def main() -> None:
    '''
    Run dump-based graph extraction.
    '''
    dumps_default = PROJECT_ROOT / 'enwiki_dumps'
    index_default = PROJECT_ROOT / 'wiki_graph_index.duckdb'
    output_default = PROJECT_ROOT / 'output'
    link_cfg_default = PROJECT_ROOT / 'resources' / 'link_types.yaml'
    perf_cfg_default = PROJECT_ROOT / 'resources' / 'perf.yaml'

    argparser = argparse.ArgumentParser(
        description='Build Wikipedia page graph from local dumps'
    )
    argparser.add_argument('-i', '--input', required=True, help='Seed title file (one Wikipedia page title per line)')
    argparser.add_argument('-o', '--output', default='required_pages.txt', help='Required pages output file')
    argparser.add_argument('--depth', type=int, required=True, help='Traversal depth')
    argparser.add_argument(
        '--dumps-dir',
        type=Path,
        default=dumps_default,
        help='Directory with enwiki dumps',
    )
    argparser.add_argument(
        '--index-db',
        type=Path,
        default=index_default,
        help='DuckDB index path',
    )
    argparser.add_argument(
        '--output-dir',
        type=Path,
        default=output_default,
        help='Directory for per-seed graphs',
    )
    argparser.add_argument(
        '--link-config',
        type=Path,
        default=link_cfg_default,
        help='Link types config path',
    )
    argparser.add_argument(
        '--perf-config',
        type=Path,
        default=perf_cfg_default,
        help='Performance config path',
    )
    argparser.add_argument(
        '--rebuild-index',
        action='store_true',
        help='Rebuild DuckDB index from dumps',
    )

    logutil.addLogArguments(argparser)
    args = argparser.parse_args()
    logutil.configureFromArgs(args)

    if args.depth < 0:
        raise ValueError('depth must be >= 0')

    with (
        open(args.input, encoding='utf-8') if args.input else sys.stdin
    ) as in_file, (
        open(args.output, 'w', encoding='utf-8') if args.output else sys.stdout
    ) as out_file:
        process(
            in_file=in_file,
            out_file=out_file,
            depth=args.depth,
            dumps_dir=args.dumps_dir,
            index_db_path=args.index_db,
            output_dir=args.output_dir,
            link_cfg_path=args.link_config,
            perf_cfg_path=args.perf_config,
            rebuild_index=args.rebuild_index,
        )


if __name__ == '__main__':
    main()
