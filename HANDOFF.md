# HANDOFF.md

## Goal

Extend the owlready2 fork with a fast, correct pyoxigraph-backed SPARQL engine to replace the rdflib adapter, while keeping the SQLite/triplelite primary store untouched.

## Current Progress

The pyoxigraph backend is fully implemented and all regression tests pass (374 tests, 3 pre-existing `rapper` CLI errors only).

**Implemented:**
- `pyoxigraph_store.py` — `OxigraphGraph` and `OxigraphContextGraph` replace `TripleLiteRDFlibGraph`. Provides `query_owlready()`, `update()`, `get_context()`, `objects()`, `triples()`, `query()`, `store.context_graphs[onto]`.
- **Store caching** — `OxigraphGraph._ox_store` caches the pyoxigraph.Store after first build. `_invalidate_cache()` is called on every write (`add()`, `remove()`, `_update()`). Speedup: 27× faster than rdflib on small ontologies, 13× on MIE-05 (1,535 triples).
- **Prefix injection** — `_inject_prefixes()` prepends `PREFIX` declarations from `bind()` bindings to every SPARQL query.
- **Inverse property resolution** — `triples()` uses `_get_obj_triples_spi_o` / `_get_obj_triples_pio_s` for properties with `owl:inverseOf`.
- `manchester.py` — Fixed `_expr_equal` / `_expr_contains` using `getattr(a, "value", None)` instead of `a.__dict__.get("value")`. This fixed `classes_matching()` on loaded ontologies (was 0/4, now 4/4).
- `namespace.py` — `World.as_rdflib_graph()` returns `OxigraphGraph(self)`.
- `test/regtest.py` — All rdflib-specific tests updated to use pyoxigraph types (`_ox.NamedNode` instead of `rdflib.URIRef`, etc.).
- `test/bench_sparql.py` — Benchmark comparing rdflib vs pyoxigraph on small test ontology and MIE-05.
- `test/test_sulo_mie.py` — 72 MIE ontology tests, all pass.

## What Worked

- Keeping SQLite/triplelite as the primary store and using pyoxigraph only for SPARQL.
- Caching the pyoxigraph.Store on `OxigraphGraph` and invalidating on writes — this was the single biggest win (turned 10× slower into 13–27× faster vs rdflib).
- `getattr()` instead of `__dict__.get()` to trigger lazy loading of restriction values.

## What Didn't Work

- **Replacing SQLite with pyoxigraph as primary store** — 241 call-sites across core modules use integer storids; persistence is the SQLite file; object cache is keyed by int storid. Not worth the rewrite cost for at most 1.8× scan speedup.
- **rdflib backend on complex ontologies** — crashes with `KeyError: rdflib.term.BNode(...)` on MIE-05 full triple dump. This is why we switched to pyoxigraph.
- Using `_build_ox_store()` on every query call — 10× slower than rdflib at 1,535 triples before caching was added.

## Next Steps

1. Consider persisting the pyoxigraph store to disk (RocksDB backend via `Store(path=...)`) to survive process restarts — currently the cache is in-memory only and rebuilt on first query after reload.
2. Explore caching per-ontology filtered stores for `OxigraphContextGraph._query()` (currently always rebuilds for `onto_filter != None`).
3. Run benchmarks on larger ontologies (10k+ triples) to validate the linear scaling assumptions.
4. Integrate the extended owlready2 into the sulo-tutorial notebooks and test all MIE 2026 demo notebooks end-to-end.
