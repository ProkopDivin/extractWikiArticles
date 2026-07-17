'''
Map Wikidata IDs to English Wikipedia page title to origin ID mapping.
'''

import argparse
import logging
import sys
from collections.abc import Iterator, Sequence
from typing import TextIO

import requests

LOG = logging.getLogger(__name__)

ENTITY_PREFIX = 'http://www.wikidata.org/entity/'
SPARQL_TIMEOUT = 60


def sparql_query(*, url: str, query: str) -> dict:
    '''
    Execute a SPARQL SELECT and return parsed JSON results.

    :param url: SPARQL endpoint URL
    :param query: SPARQL query string
    :return: parsed JSON response
    '''
    resp = requests.post(
        url,
        data={'query': query},
        headers={
            'Accept': 'application/sparql-results+json',
            'User-Agent': 'extractWikiArticles/1.0 (https://github.com/ProkopDivin/entity-enhance-classification)',
        },
        timeout=SPARQL_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def iter_qids(*, in_file: TextIO) -> Iterator[str]:
    '''
    Stream QIDs from input file.

    :param in_file: input text stream with one QID per line
    :return: iterator of normalized QIDs
    '''
    for raw_line in in_file:
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue

        qid = line.split('\t', maxsplit=1)[0].strip()
        if not qid:
            continue

        if qid.startswith('http://www.wikidata.org/entity/'):
            qid = qid.rsplit('/', maxsplit=1)[-1]

        if qid.startswith('Q'):
            yield qid


def iter_batches(
    *,
    qids: Iterator[str],
    batch_size: int,
) -> Iterator[list[str]]:
    '''
    Yield QIDs in fixed-size batches.

    :param qids: input QID iterator
    :param batch_size: max number of QIDs in one batch
    :return: iterator of QID batches
    '''
    batch: list[str] = []
    for qid in qids:
        batch.append(qid)
        if len(batch) >= batch_size:
            yield batch
            batch = []

    if batch:
        yield batch


def build_enwiki_query(*, qids: Sequence[str]) -> str:
    '''
    Build SPARQL query returning enwiki page titles for QIDs.

    :param qids: Wikidata IDs
    :return: query string
    '''
    items = ' '.join(f'wd:{qid}' for qid in qids)
    return f'''
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX schema: <http://schema.org/>
SELECT ?item ?title
WHERE {{
  VALUES ?item {{ {items} }}
  ?enwiki schema:about ?item ;
          schema:isPartOf <https://en.wikipedia.org/> ;
          schema:name ?title .
  FILTER(LANG(?title) = 'en')
}}
ORDER BY ?item
'''


def build_parent_query(*, qids: Sequence[str], prop: str) -> str:
    '''
    Build SPARQL query for parent relation.

    :param qids: Wikidata IDs
    :param prop: property ID, e.g. P31 or P279
    :return: query string
    '''
    items = ' '.join(f'wd:{qid}' for qid in qids)
    return f'''
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
SELECT ?item ?parent
WHERE {{
  VALUES ?item {{ {items} }}
  ?item wdt:{prop} ?parent .
}}
ORDER BY ?item ?parent
'''


def qid_from_item_uri(*, item_uri: str) -> str | None:
    '''
    Parse QID from Wikidata entity URI.

    :param item_uri: full entity URI
    :return: QID or None
    '''
    if not item_uri.startswith(ENTITY_PREFIX):
        return None
    qid = item_uri[len(ENTITY_PREFIX):]
    return qid or None


def uniq_list(*, values: Sequence[str]) -> list[str]:
    '''
    Keep first occurrence order and remove duplicates.

    :param values: input values
    :return: unique values in original order
    '''
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def fetch_enwiki_map(*, sparql_url: str, qids: Sequence[str]) -> dict[str, str]:
    '''
    Fetch English Wikipedia page titles for input QIDs.

    :param sparql_url: SPARQL endpoint URL
    :param qids: Wikidata IDs
    :return: mapping qid -> enwiki_title
    '''
    if not qids:
        return {}

    payload = sparql_query(url=sparql_url, query=build_enwiki_query(qids=qids))
    out: dict[str, str] = {}
    for row in payload.get('results', {}).get('bindings', []):
        item_uri = row.get('item', {}).get('value')
        title = row.get('title', {}).get('value')
        if not item_uri or not title:
            continue

        qid = qid_from_item_uri(item_uri=item_uri)
        if qid and qid not in out:
            out[qid] = title
    return out


def fetch_parent_map(
    *,
    sparql_url: str,
    qids: Sequence[str],
    prop: str,
) -> dict[str, list[str]]:
    '''
    Fetch parent mapping for one relation.

    :param sparql_url: SPARQL endpoint URL
    :param qids: Wikidata IDs
    :param prop: property ID
    :return: mapping child_qid -> list[parent_qid]
    '''
    if not qids:
        return {}

    payload = sparql_query(url=sparql_url, query=build_parent_query(qids=qids, prop=prop))
    out: dict[str, list[str]] = {}
    for row in payload.get('results', {}).get('bindings', []):
        item_uri = row.get('item', {}).get('value')
        parent_uri = row.get('parent', {}).get('value')
        if not item_uri or not parent_uri:
            continue

        item_qid = qid_from_item_uri(item_uri=item_uri)
        parent_qid = qid_from_item_uri(item_uri=parent_uri)
        if not item_qid or not parent_qid:
            continue

        out.setdefault(item_qid, []).append(parent_qid)

    for key, values in list(out.items()):
        out[key] = uniq_list(values=values)

    return out


def resolve_with_parent_fallback(
    *,
    sparql_url: str,
    qids: Sequence[str],
    max_depth: int,
) -> tuple[dict[str, list[str]], dict[str, str], int]:
    '''
    Resolve representative QID by direct match or parent fallback.

    Parent expansion rule per node:
    - use P31 parents when available
    - otherwise use P279 parents

    :param sparql_url: SPARQL endpoint URL
    :param qids: original Wikidata IDs
    :param max_depth: max fallback depth
    :return: (mapping qid -> representative_qids, qid_to_enwiki_title, fallback_hits_count)
    '''
    ordered_qids = uniq_list(values=qids)
    title_map = fetch_enwiki_map(sparql_url=sparql_url, qids=ordered_qids)
    qids_with_page = set(title_map.keys())
    resolved: dict[str, list[str]] = {
        qid: [qid] for qid in ordered_qids if qid in qids_with_page
    }
    fallback_hits = 0

    unresolved = [qid for qid in ordered_qids if qid not in resolved]
    if not unresolved or max_depth <= 0:
        return resolved, title_map, fallback_hits

    frontier_map: dict[str, list[str]] = {qid: [qid] for qid in unresolved}
    visited_map: dict[str, set[str]] = {qid: {qid} for qid in unresolved}

    for _depth in range(1, max_depth + 1):
        unresolved_now = [qid for qid in unresolved if qid not in resolved]
        if not unresolved_now:
            break

        current_nodes: list[str] = []
        for qid in unresolved_now:
            current_nodes.extend(frontier_map.get(qid, []))
        current_nodes = uniq_list(values=current_nodes)
        if not current_nodes:
            break

        parents_p31 = fetch_parent_map(sparql_url=sparql_url, qids=current_nodes, prop='P31')
        parents_p279 = fetch_parent_map(sparql_url=sparql_url, qids=current_nodes, prop='P279')

        next_nodes_all: list[str] = []
        for qid in unresolved_now:
            next_nodes: list[str] = []
            for node in frontier_map.get(qid, []):
                parent_nodes = parents_p31.get(node, []) or parents_p279.get(node, [])
                for parent_qid in parent_nodes:
                    if parent_qid in visited_map[qid]:
                        continue
                    visited_map[qid].add(parent_qid)
                    next_nodes.append(parent_qid)
            frontier_map[qid] = next_nodes
            next_nodes_all.extend(next_nodes)

        next_nodes_all = uniq_list(values=next_nodes_all)
        if not next_nodes_all:
            break

        parent_title_map = fetch_enwiki_map(sparql_url=sparql_url, qids=next_nodes_all)
        title_map.update(parent_title_map)
        parent_qids_with_page = set(parent_title_map.keys())
        for qid in unresolved_now:
            if qid in resolved:
                continue
            # Keep all representatives found at the first
            # possible depth for this origin QID.
            candidates = [
                parent_qid
                for parent_qid in frontier_map.get(qid, [])
                if parent_qid in parent_qids_with_page
            ]
            if candidates:
                resolved[qid] = uniq_list(values=candidates)
                fallback_hits += 1

    return resolved, title_map, fallback_hits


def process(
    *,
    in_file: TextIO,
    out_file: TextIO,
    sparql_url: str,
    batch_size: int,
    max_depth: int,
) -> None:
    '''
    Query enwiki graph and write enwiki_title-to-origin mapping.

    :param in_file: input stream with Wikidata IDs
    :param out_file: output stream for mappings
    :param sparql_url: SPARQL endpoint URL
    :param batch_size: number of QIDs per request
    :param max_depth: max fallback depth
    '''
    qid_stream = iter_qids(in_file=in_file)

    total_qids = 0
    mapped_qids = 0
    direct_qids = 0
    fallback_qids = 0
    seen_qids: set[str] = set()

    for batch_idx, batch in enumerate(
        iter_batches(qids=qid_stream, batch_size=batch_size),
        start=1,
    ):
        total_qids += len(batch)
        try:
            batch_map, title_map, _batch_fallback = resolve_with_parent_fallback(
                sparql_url=sparql_url,
                qids=batch,
                max_depth=max_depth,
            )
        except Exception:
            LOG.exception(
                f'SPARQL query failed, batch_idx={batch_idx}, batch_size={len(batch)}'
            )
            raise

        for qid in batch:
            representative_qids = batch_map.get(qid, [])
            if not representative_qids or qid in seen_qids:
                continue
            enwiki_titles = uniq_list(
                values=[
                    enwiki_title
                    for representative_qid in representative_qids
                    if (enwiki_title := title_map.get(representative_qid))
                ]
            )
            if not enwiki_titles:
                LOG.warning(
                    f'Skipping unresolved enwiki title, qid={qid}, reps={",".join(representative_qids)}'
                )
                continue
            seen_qids.add(qid)
            mapped_qids += 1
            if representative_qids == [qid]:
                direct_qids += 1
            for enwiki_title in enwiki_titles:
                out_file.write(f'{enwiki_title}\t{qid}\n')

        fallback_qids = mapped_qids - direct_qids

        LOG.info(
            f'Processed batch_idx={batch_idx}, '
            f'input_qids={total_qids}, mapped_qids={mapped_qids}, '
            f'direct_qids={direct_qids}, fallback_qids={fallback_qids}'
        )

    missing_qids = total_qids - mapped_qids
    LOG.info(
        f'Completed enwiki mapping, total_qids={total_qids}, '
        f'mapped_qids={mapped_qids}, direct_qids={direct_qids}, '
        f'fallback_qids={fallback_qids}, missing_qids={missing_qids}'
    )


def main() -> None:
    '''
    Run enwiki mapping pipeline.
    '''
    argparser = argparse.ArgumentParser(
        description='Map English Wikipedia page titles to origin Wikidata IDs'
    )
    argparser.add_argument('-i', '--input', default='wdId_ids.txt')
    argparser.add_argument('-o', '--output', default='wdid2wiki.txt')
    argparser.add_argument(
        '--sparql-url',
        default='https://query.wikidata.org/sparql',
    )
    argparser.add_argument('--batch-size', type=int, default=200)
    argparser.add_argument('--max-depth', type=int, default=3)
    argparser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])

    args = argparser.parse_args()
    logging.basicConfig(level=args.log_level, format='%(levelname)s %(name)s: %(message)s')

    if args.batch_size <= 0:
        raise ValueError('batch-size must be > 0')
    if args.max_depth < 0:
        raise ValueError('max-depth must be >= 0')

    with (
        open(args.input, encoding='utf-8') if args.input else sys.stdin
    ) as in_file, (
        open(args.output, 'w', encoding='utf-8') if args.output else sys.stdout
    ) as out_file:
        process(
            in_file=in_file,
            out_file=out_file,
            sparql_url=args.sparql_url,
            batch_size=args.batch_size,
            max_depth=args.max_depth,
        )


if __name__ == '__main__':
    main()
