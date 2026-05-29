# -*- coding: utf-8 -*-
# Owlready2
# Copyright (C) 2017-2019 Jean-Baptiste LAMY
# LIMICS (Laboratoire d'informatique médicale et d'ingénierie des connaissances en santé), UMR_S 1142
# University Paris 13, Sorbonne paris-Cité, Bobigny, France
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
pyoxigraph-backed SPARQL engine for owlready2.

Replaces the rdflib adapter (rdflib_store.py).  A pyoxigraph.Store is built
from triplelite on the first SPARQL call and then cached on OxigraphGraph.
The cache is invalidated whenever a triple is added or removed (via add(),
remove(), or _update()), so the next query rebuilds it from the current
triplelite state.

Public surface kept intentionally identical to the old rdflib-based one so
that existing callers of world.as_sparql_graph() / world.sparql_query() keep
working without changes.
"""

import pyoxigraph as _ox

import owlready2.namespace as _ns
from owlready2.base import from_literal

# ── blank-node ID encoding ────────────────────────────────────────────────────
# triplelite uses negative integers for blank nodes.
# We encode them as the decimal string of their absolute value.

def _bn_encode(neg_storid):
    return str(-neg_storid)

def _bn_decode(bnode):
    return -int(bnode.value)


# ── term helpers ──────────────────────────────────────────────────────────────

def _term_iri(node):
    """Extract the IRI string from a pyoxigraph NamedNode or any string-like."""
    if isinstance(node, _ox.NamedNode):
        return node.value
    return str(node)

def _node_to_storid(node, graph):
    """Convert a pyoxigraph NamedNode/BlankNode to a triplelite storid."""
    if isinstance(node, _ox.BlankNode):
        return _bn_decode(node)
    return graph._abbreviate(_term_iri(node))

def _ox_literal_to_raw(lit, graph):
    """Convert a pyoxigraph Literal to (value_str, dtype_storid)."""
    lang = getattr(lit, "language", None)
    if lang:
        return lit.value, "@" + lang
    dt = getattr(lit, "datatype", None)
    if dt:
        return lit.value, graph._abbreviate(dt.value, create_if_missing=True)
    return lit.value, 0

def _inject_prefixes(prefixes, query):
    """Prepend PREFIX declarations from the bound prefix dict to a SPARQL string."""
    if not prefixes:
        return query
    lines = "\n".join(f"PREFIX {p}: <{ns}>" for p, ns in prefixes.items())
    return lines + "\n" + query


# ── term converters: triplelite ↔ pyoxigraph ─────────────────────────────────

def _storid_to_ox(storid, world):
    """Convert a triplelite subject/predicate/object storid to a pyoxigraph term."""
    if storid < 0:
        return _ox.BlankNode(_bn_encode(storid))
    return _ox.NamedNode(world.graph._unabbreviate(storid))


def _literal_to_ox(value, dtype, world):
    """Convert a triplelite literal (value, dtype) to a pyoxigraph Literal."""
    if isinstance(dtype, str) and dtype.startswith("@"):
        return _ox.Literal(str(value), language=dtype[1:])
    if dtype == 0 or dtype == "" or dtype is None:
        return _ox.Literal(str(value))
    return _ox.Literal(str(value), datatype=_ox.NamedNode(world.graph._unabbreviate(dtype)))


def _ox_to_owlready(term, world):
    """Convert a pyoxigraph result term to an owlready2 object / Python literal."""
    if isinstance(term, _ox.NamedNode):
        return world[term.value]
    if isinstance(term, _ox.BlankNode):
        storid = _bn_decode(term)
        # Try to find the bnode across all ontologies
        for onto in world.ontologies.values():
            if storid in onto._bnodes:
                return onto._bnodes[storid]
        return storid
    if isinstance(term, _ox.Literal):
        lang = getattr(term, "language", None)
        if lang:
            return from_literal(term.value, "@" + lang)
        dt = getattr(term, "datatype", None)
        if dt:
            abbrev = world.graph._abbreviate(dt.value, create_if_missing=False)
            return from_literal(term.value, abbrev if abbrev else dt.value)
        return from_literal(term.value, "")
    return None


# ── store builder ─────────────────────────────────────────────────────────────

def _build_ox_store(world, onto_filter=None):
    """
    Build and return a pyoxigraph.Store populated from triplelite.

    Triples are added to BOTH the default graph (so plain SELECT patterns
    match without GRAPH clauses) and to named graphs keyed by ontology IRI
    (so GRAPH-pattern queries and UPDATE context tracking work).

    If *onto_filter* is not None, only triples from that ontology are loaded.
    """
    ox_store = _ox.Store()
    graph    = world.graph
    default  = _ox.DefaultGraph()

    ontos = ([onto_filter] if onto_filter else
             list(world.ontologies.values()))

    for onto in ontos:
        subgraph = graph.onto_2_subgraph.get(onto)
        if subgraph is None:
            continue
        g_node = _ox.NamedNode(onto.base_iri)

        # object triples
        for s, p, o in subgraph._get_obj_triples_spo_spo(None, None, None):
            try:
                ox_s = _storid_to_ox(s, world)
                ox_p = _storid_to_ox(p, world)
                ox_o = _storid_to_ox(o, world)
                ox_store.add(_ox.Quad(ox_s, ox_p, ox_o, default))
                ox_store.add(_ox.Quad(ox_s, ox_p, ox_o, g_node))
            except Exception:
                pass

        # data triples
        for s, p, o, d in subgraph._get_data_triples_spod_spod(None, None, None, None):
            try:
                ox_s = _storid_to_ox(s, world)
                ox_p = _storid_to_ox(p, world)
                ox_o = _literal_to_ox(o, d, world)
                ox_store.add(_ox.Quad(ox_s, ox_p, ox_o, default))
                ox_store.add(_ox.Quad(ox_s, ox_p, ox_o, g_node))
            except Exception:
                pass

    return ox_store


# ── UPDATE diff → triplelite sync ─────────────────────────────────────────────

def _apply_ox_diff(added, removed, world, onto_filter=None):
    """Write the quad diff produced by a SPARQL UPDATE back into triplelite."""
    graph = world.graph

    def _resolve_context(graph_name):
        """Map an ox graph_name to a triplelite subgraph."""
        if isinstance(graph_name, _ox.DefaultGraph):
            # No explicit graph — use the active ontology or the filter
            l = _ns.CURRENT_NAMESPACES.get()
            if l:
                return l[-1].ontology.graph
            if onto_filter:
                return graph.onto_2_subgraph.get(onto_filter)
            return None
        iri = graph_name.value
        for onto, sg in graph.onto_2_subgraph.items():
            if onto.base_iri == iri or onto.base_iri.rstrip("/#") == iri:
                return sg
        return None

    def _quad_to_raw(q):
        s_node = q.subject
        p_node = q.predicate
        o_node = q.object
        if isinstance(s_node, _ox.NamedNode):
            s = graph._abbreviate(s_node.value)
        else:
            s = _bn_decode(s_node)
        p = graph._abbreviate(p_node.value)
        if isinstance(o_node, _ox.NamedNode):
            return s, p, graph._abbreviate(o_node.value), None
        if isinstance(o_node, _ox.BlankNode):
            return s, p, _bn_decode(o_node), None
        # Literal
        lang = getattr(o_node, "language", None)
        dt   = getattr(o_node, "datatype", None)
        if lang:
            d = "@" + lang
            o = o_node.value
        elif dt:
            d = graph._abbreviate(dt.value)
            o = o_node.value
        else:
            d = 0
            o = o_node.value
        return s, p, o, d

    for q in added:
        sg = _resolve_context(q.graph_name)
        if sg is None:
            continue
        s, p, o, d = _quad_to_raw(q)
        if d is None:
            sg._add_obj_triple_raw_spo(s, p, o)
        else:
            sg._add_data_triple_raw_spod(s, p, o, d)

    for q in removed:
        sg = _resolve_context(q.graph_name)
        if sg is None:
            continue
        s, p, o, d = _quad_to_raw(q)
        if d is None:
            sg._del_obj_triple_raw_spo(s, p, o)
        else:
            sg._del_data_triple_raw_spod(s, p, o, d)


# ── store proxy ───────────────────────────────────────────────────────────────

class _OxigraphContextGraphsProxy:
    """Proxy for g.store.context_graphs[onto] access."""
    def __init__(self, graph):
        self._graph = graph

    def __getitem__(self, onto):
        return self._graph.get_context(onto)


class _OxigraphStoreProxy:
    """Proxy for g.store, exposes context_graphs."""
    def __init__(self, graph):
        self.context_graphs = _OxigraphContextGraphsProxy(graph)


# ── public graph objects ──────────────────────────────────────────────────────

class OxigraphContextGraph:
    """
    Represents a single-ontology named graph.
    Returned by OxigraphGraph.get_context().
    """

    def __init__(self, main_graph, onto):
        self._main  = main_graph
        self._onto  = onto
        self._world = main_graph._world

    def update(self, query):
        self._main._update(query, onto_filter=self._onto)

    def query_owlready(self, query):
        yield from self._main._query(query, onto_filter=self._onto)

    def add(self, triple):
        """Add a triple (pyoxigraph terms) to this ontology's subgraph."""
        s_node, p_node, o_node = triple
        world = self._world
        graph = world.graph
        sg = graph.onto_2_subgraph.get(self._onto)
        if sg is None:
            return
        s = _node_to_storid(s_node, graph)
        p = _node_to_storid(p_node, graph)
        if isinstance(o_node, _ox.Literal):
            o, d = _ox_literal_to_raw(o_node, graph)
            sg._add_data_triple_raw_spod(s, p, o, d)
        else:
            o = _node_to_storid(o_node, graph)
            sg._add_obj_triple_raw_spo(s, p, o)
        self._main._invalidate_cache()

    def remove(self, triple):
        """Remove matching triples; object position may be None (wildcard)."""
        s_node, p_node, o_node = triple
        world = self._world
        graph = world.graph
        sg = graph.onto_2_subgraph.get(self._onto)
        if sg is None:
            return
        s = _node_to_storid(s_node, graph)
        p = _node_to_storid(p_node, graph)
        if o_node is None:
            for o in list(sg._get_obj_triples_sp_o(s, p)):
                sg._del_obj_triple_raw_spo(s, p, o)
            for o, d in list(sg._get_data_triples_sp_od(s, p)):
                sg._del_data_triple_raw_spod(s, p, o, d)
        elif isinstance(o_node, _ox.Literal):
            o, d = _ox_literal_to_raw(o_node, graph)
            sg._del_data_triple_raw_spod(s, p, o, d)
        else:
            o = _node_to_storid(o_node, graph)
            sg._del_obj_triple_raw_spo(s, p, o)
        self._main._invalidate_cache()


class OxigraphGraph:
    """
    Drop-in replacement for TripleLiteRDFlibGraph.

    Provides the same interface that callers of world.as_sparql_graph() expect:
      g.bind(prefix, namespace)
      g.query_owlready(sparql)
      g.update(sparql)
      g.get_context(onto_or_iri)
      g.objects(s, p)
      g.triples(pattern)
      g.query(sparql)
      g.store.context_graphs[onto]
    """

    def __init__(self, world):
        self._world        = world
        self._prefixes     = {}
        self._contexts     = {}   # onto → OxigraphContextGraph
        self._store_proxy  = _OxigraphStoreProxy(self)
        self._ox_store     = None  # cached pyoxigraph.Store; None = dirty
        # Strong-reference cache: IRI string → owlready2 entity.
        # Prevents WeakValueDictionary in _entities_cache from evicting live
        # query results between iterations, eliminating repeated SQLite loads.
        self._entity_cache: dict = {}

    # ── namespace binding ─────────────────────────────────────────────────────

    def bind(self, prefix, namespace):
        self._prefixes[prefix] = str(namespace)

    # ── store property ────────────────────────────────────────────────────────

    @property
    def store(self):
        return self._store_proxy

    # ── store cache ───────────────────────────────────────────────────────────

    def _get_cached_store(self):
        """Return the cached full-world pyoxigraph.Store, building it if dirty."""
        if self._ox_store is None:
            self._ox_store = _build_ox_store(self._world)
        return self._ox_store

    def _invalidate_cache(self):
        """Mark the store cache dirty so it is rebuilt on the next query."""
        self._ox_store = None
        self._entity_cache.clear()

    # ── context access ────────────────────────────────────────────────────────

    def get_context(self, onto_or_iri):
        """Return an OxigraphContextGraph for the given ontology or base IRI."""
        world = self._world
        onto  = onto_or_iri
        if hasattr(onto_or_iri, "base_iri"):
            # Already an owlready2 Ontology object
            pass
        elif isinstance(onto_or_iri, str):
            iri = onto_or_iri
            onto = None
            for o in world.ontologies.values():
                if o.base_iri == iri or o.base_iri.rstrip("/#") == iri:
                    onto = o
                    break
            if onto is None:
                raise ValueError(f"No ontology with IRI {iri!r}")
        else:
            # str-like (e.g. anything with __str__)
            return self.get_context(str(onto_or_iri))
        if onto not in self._contexts:
            self._contexts[onto] = OxigraphContextGraph(self, onto)
        return self._contexts[onto]

    # ── internal SPARQL helpers ───────────────────────────────────────────────

    def _query(self, query, onto_filter=None):
        """Execute SPARQL SELECT/ASK; return list of translated rows (not a generator)."""
        ox_store = (self._get_cached_store() if onto_filter is None
                    else _build_ox_store(self._world, onto_filter))
        result = ox_store.query(_inject_prefixes(self._prefixes, query))

        if isinstance(result, _ox.QueryBoolean):
            return [[bool(result)]]

        world      = self._world
        cache      = self._entity_cache
        _NamedNode = _ox.NamedNode
        abbrev_d   = world.graph._abbreviate_d

        if abbrev_d is not None:
            # Dict mode (threshold ≤ 2M resources): two direct dict hits per IRI.
            entities = world._entities

            def _convert(term):
                if term.__class__ is not _NamedNode:
                    return _ox_to_owlready(term, world)
                iri = term.value
                if iri in cache:
                    return cache[iri]
                storid = abbrev_d.get(iri)
                if storid is None:
                    obj = None
                else:
                    obj = entities.get(storid)
                    if obj is None:
                        obj = world._get_by_storid(storid, iri)
                cache[iri] = obj
                return obj
        else:
            # SQL mode (>2M resources): batch-resolve all unseen IRIs up front so
            # the convert loop below stays a simple cache hit.
            rows = list(result)
            new_iris = list(dict.fromkeys(           # deduplicate, preserve order
                term.value
                for row in rows
                for term in row
                if term.__class__ is _NamedNode and term.value not in cache
            ))
            graph = world.graph
            for i in range(0, len(new_iris), 900):   # SQLite variable limit
                batch = new_iris[i:i + 900]
                placeholders = ','.join('?' * len(batch))
                for iri, storid in graph.execute(
                        f"SELECT iri, storid FROM resources WHERE iri IN ({placeholders})",
                        batch).fetchall():
                    cache[iri] = world._get_by_storid(storid, iri)
            result = rows                             # already materialised

            def _convert(term):
                if term.__class__ is not _NamedNode:
                    return _ox_to_owlready(term, world)
                iri = term.value
                if iri not in cache:
                    cache[iri] = world[iri]
                return cache[iri]

        return [[_convert(term) for term in sol] for sol in result]

    def _update(self, query, onto_filter=None):
        """Execute a SPARQL UPDATE and sync the diff back to triplelite."""
        if onto_filter is None:
            ox_store = self._get_cached_store()
        else:
            ox_store = _build_ox_store(self._world, onto_filter)
        before   = set(ox_store.quads_for_pattern(None, None, None, None))
        ox_store.update(_inject_prefixes(self._prefixes, query))
        after    = set(ox_store.quads_for_pattern(None, None, None, None))
        _apply_ox_diff(after - before, before - after, self._world, onto_filter)
        # Triplelite was modified; invalidate so the next query rebuilds from it.
        self._invalidate_cache()

    # ── triplelite-level iteration (no SPARQL overhead) ───────────────────────

    def objects(self, s, p):
        """Yield all objects for the given subject and predicate (pyoxigraph terms)."""
        world = self._world
        graph = world.graph
        s_id  = graph._abbreviate(_term_iri(s), create_if_missing=False)
        p_id  = graph._abbreviate(_term_iri(p), create_if_missing=False)
        if s_id is None or p_id is None:
            return
        for o in graph._get_obj_triples_sp_o(s_id, p_id):
            yield _storid_to_ox(o, world)
        for o, d in graph._get_data_triples_sp_od(s_id, p_id):
            yield _literal_to_ox(o, d, world)

    def triples(self, pattern):
        """Yield (s, p, o) tuples of pyoxigraph terms matching the pattern.
        Any position may be None to act as a wildcard.
        Inverse properties (owl:inverseOf) are resolved so that implied triples
        are returned alongside stored ones."""
        from owlready2.base import owl_inverse_property
        world  = self._world
        graph  = world.graph
        s_node, p_node, o_node = pattern

        def _to_id(node):
            if node is None:
                return None
            return graph._abbreviate(_term_iri(node), create_if_missing=False)

        s_id = _to_id(s_node)
        p_id = _to_id(p_node)

        # ── Object triples (skip if o_node is a Literal) ─────────────────────
        if o_node is None or not isinstance(o_node, _ox.Literal):
            o_id = _node_to_storid(o_node, graph) if o_node is not None else None

            if p_id is not None:
                # Fixed predicate: use inverse-aware method when available
                inv_id = graph._get_obj_triple_sp_o(p_id, owl_inverse_property)
                if inv_id is not None:
                    if s_id is not None:
                        # _get_obj_triples_spi_o returns effective objects of p_id from s_id,
                        # including triples implied by the inverse property inv_id
                        for o in graph._get_obj_triples_spi_o(s_id, p_id, inv_id):
                            if o_id is None or o == o_id:
                                yield (_storid_to_ox(s_id, world),
                                       _storid_to_ox(p_id, world),
                                       _storid_to_ox(o, world))
                    else:
                        # Wildcard s: _get_obj_triples_pio_s returns effective subjects
                        for s in graph._get_obj_triples_pio_s(p_id, inv_id, o_id):
                            yield (_storid_to_ox(s, world),
                                   _storid_to_ox(p_id, world),
                                   _storid_to_ox(o_id, world))
                else:
                    for s, p, o in graph._get_obj_triples_spo_spo(s_id, p_id, o_id):
                        yield (_storid_to_ox(s, world),
                               _storid_to_ox(p, world),
                               _storid_to_ox(o, world))
            else:
                # Wildcard predicate: yield direct triples first
                for s, p, o in graph._get_obj_triples_spo_spo(s_id, None, o_id):
                    yield (_storid_to_ox(s, world),
                           _storid_to_ox(p, world),
                           _storid_to_ox(o, world))
                # Then yield inverse-inferred triples when s is fixed:
                # find stored triples where s_id appears as object (or o_id as subject)
                if s_id is not None:
                    for s2, p2, o2 in graph._get_obj_triples_spo_spo(o_id, None, s_id):
                        p_inv = graph._get_obj_triple_sp_o(p2, owl_inverse_property)
                        if p_inv is not None:
                            yield (_storid_to_ox(s_id, world),
                                   _storid_to_ox(p_inv, world),
                                   _storid_to_ox(s2, world))

        # ── Data triples (skip if o_node is a NamedNode/BlankNode) ───────────
        if o_node is None or isinstance(o_node, _ox.Literal):
            o_val = o_node.value if isinstance(o_node, _ox.Literal) else None
            for s, p, o, d in graph._get_data_triples_spod_spod(s_id, p_id, o_val, None):
                yield (_storid_to_ox(s, world),
                       _storid_to_ox(p, world),
                       _literal_to_ox(o, d, world))

    # ── public API ────────────────────────────────────────────────────────────

    def query_owlready(self, query):
        return self._query(query)

    def update(self, query):
        self._update(query)

    def query(self, sparql):
        """Execute SPARQL SELECT/ASK, yield tuples of raw pyoxigraph terms."""
        full_query = _inject_prefixes(self._prefixes, sparql)
        ox_store   = self._get_cached_store()
        result     = ox_store.query(full_query)
        if isinstance(result, _ox.QueryBoolean):
            yield (bool(result),)
            return
        for sol in result:
            yield tuple(sol)
