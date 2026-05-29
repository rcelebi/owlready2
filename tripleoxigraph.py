# -*- coding: utf-8 -*-
"""
Oxigraph (pyoxigraph) backend for owlready2.

Usage:
    world = owlready2.World()
    world.set_backend("oxigraph")                        # in-memory
    world.set_backend("oxigraph", filename="path.oxg")   # persistent
"""

import os
import time
import threading
import sqlite3

try:
    import pyoxigraph
    _OXIGRAPH_AVAILABLE = True
except ImportError:
    _OXIGRAPH_AVAILABLE = False

if _OXIGRAPH_AVAILABLE:
    from owlready2.driver import BaseMainGraph, BaseSubGraph, _save
    from owlready2.base import (
        _universal_abbrev_2_iri, _universal_iri_2_abbrev,
        rdf_type, owl_ontology,
        SOME, ONLY, VALUE,
        owl_onclass, owl_onproperty, owl_complementof,
        owl_inverse_property, owl_ondatarange,
        owl_annotatedsource, owl_annotatedproperty, owl_annotatedtarget,
        rdf_first, rdf_rest, rdf_nil,
    )
    from owlready2.driver import INT_DATATYPES, FLOAT_DATATYPES
    from collections import defaultdict as _defaultdict


    # -----------------------------------------------------------------------
    # Main graph
    # -----------------------------------------------------------------------

    class OxigraphGraph(BaseMainGraph):
        _SUPPORT_CLONING = False

        def __init__(self, filename=None, world=None):
            if filename is None or filename == ":memory:":
                self._store = pyoxigraph.Store()
            else:
                self._store = pyoxigraph.Store(path=filename)

            self.world          = world
            self._lock          = threading.RLock()
            self.indexed        = True   # pyoxigraph has its own efficient indices

            # storid <-> IRI maps (positive storids only; blanks are negative)
            self._iri_2_storid  = {}
            self._storid_2_iri  = {}
            # non-numeric blank node strings -> negative storid
            self._blank_2_storid = {}

            # Seed with all universal abbreviations
            _max_universal = 0
            for iri, storid in _universal_iri_2_abbrev.items():
                if isinstance(storid, int):
                    self._iri_2_storid[iri]    = storid
                    self._storid_2_iri[storid] = iri
                    if storid > _max_universal:
                        _max_universal = storid

            self.current_resource = max(_max_universal + 1, 300)
            self.current_blank    = 0  # incremented before use → first blank is -1

            # Context/ontology management
            self.ontologies        = []       # [(c_storid, iri), …]
            self._c_2_onto         = {}       # c_storid -> Ontology object
            self._c_2_graphname    = {}       # c_storid -> pyoxigraph.NamedNode
            self._graphname_to_c   = {}       # graph IRI string -> c_storid
            self._ontology_aliases = {}       # actual_iri -> requested_iri
            self._last_update_time = {}       # c_storid -> float timestamp

            self.onto_2_subgraph = {}       # onto -> OxigraphSubGraph
            self.c               = None     # main graph has no single context

            # needed by BaseSubGraph.parse()
            self.last_numbered_iri = {}

            # Shadow in-memory SQLite for search queries
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
            self._db.execute("CREATE TABLE objs (c INTEGER, s INTEGER, p INTEGER, o INTEGER)")
            self._db.execute("CREATE TABLE datas (c INTEGER, s INTEGER, p INTEGER, o BLOB, d INTEGER)")
            self._db.execute("CREATE VIEW quads AS SELECT c,s,p,o,NULL AS d FROM objs UNION ALL SELECT c,s,p,o,d FROM datas")
            self._db.execute("CREATE TABLE resources (storid INTEGER PRIMARY KEY, iri TEXT)")
            self._db.execute("CREATE UNIQUE INDEX index_resources_iri ON resources(iri)")
            # Separate ontologies table (like triplelite) — c values are independent of storids
            self._db.execute("CREATE TABLE ontologies (c INTEGER PRIMARY KEY AUTOINCREMENT, iri TEXT UNIQUE)")
            self._db.execute("CREATE INDEX index_objs_sp ON objs(s,p)")
            self._db.execute("CREATE UNIQUE INDEX index_objs_op ON objs(o,p,c,s)")
            self._db.execute("CREATE INDEX index_datas_sp ON datas(s,p)")
            self._db.execute("CREATE UNIQUE INDEX index_datas_op ON datas(o,p,c,d,s)")
            for iri, storid in _universal_iri_2_abbrev.items():
                if isinstance(storid, int) and isinstance(iri, str):
                    self._db.execute("INSERT OR IGNORE INTO resources (storid, iri) VALUES (?, ?)", (storid, iri))
            self._db.commit()

            # If store already has data (persistent re-open), rebuild in-memory state
            if next(self._store.quads_for_pattern(None, None, None, None), None) is not None:
                self._rebuild_from_store()

        def _rebuild_from_store(self):
            """Reconstruct in-memory IRI maps, named-graph index and shadow SQLite from existing pyoxigraph store."""
            db = self._db

            # Discover named graphs → populate ontologies / graph maps
            for gn in self._store.named_graphs():
                if isinstance(gn, pyoxigraph.NamedNode):
                    g_iri = gn.value
                    c = self._c_allocate(g_iri)
                    if g_iri not in self._graphname_to_c:
                        self._c_2_graphname[c]   = gn
                        self._graphname_to_c[g_iri] = c
                    # Register as known ontology
                    onto_iri = g_iri
                    if not any(i == onto_iri for _, i in self.ontologies):
                        self.ontologies.append((c, onto_iri))

            # Walk all quads and rebuild shadow SQLite + ensure all IRIs are abbreviated
            obj_rows  = []
            data_rows = []

            for quad in self._store.quads_for_pattern(None, None, None, None):
                gn = quad.graph_name
                if not isinstance(gn, pyoxigraph.NamedNode):
                    continue
                c2 = self._graphname_to_c.get(gn.value)
                if c2 is None:
                    continue
                s2 = self._node_to_storid(quad.subject)
                p2 = self._node_to_storid(quad.predicate)
                if s2 is None or p2 is None:
                    continue

                if isinstance(quad.object, pyoxigraph.Literal):
                    o2, d2 = self._literal_to_val(quad.object)
                    data_rows.append((c2, s2, p2, o2, d2))
                elif isinstance(quad.object, (pyoxigraph.NamedNode, pyoxigraph.BlankNode)):
                    o2 = self._node_to_storid(quad.object)
                    if o2 is not None:
                        obj_rows.append((c2, s2, p2, o2))

            db.executemany("INSERT OR IGNORE INTO objs (c,s,p,o) VALUES (?,?,?,?)", obj_rows)
            db.executemany("INSERT OR IGNORE INTO datas (c,s,p,o,d) VALUES (?,?,?,?,?)", data_rows)
            db.commit()

        # -------------------------------------------------------------------
        # storid <-> IRI
        # -------------------------------------------------------------------

        def _abbreviate(self, iri, create_if_missing=True):
            storid = _universal_iri_2_abbrev.get(iri)
            if storid is not None:
                return storid
            storid = self._iri_2_storid.get(iri)
            if storid is not None:
                return storid
            if not create_if_missing:
                return None
            with self._lock:
                storid = self._iri_2_storid.get(iri)
                if storid is not None:
                    return storid
                self.current_resource += 1
                storid = self.current_resource
                self._iri_2_storid[iri]    = storid
                self._storid_2_iri[storid] = iri
                self._db.execute("INSERT OR IGNORE INTO resources (storid, iri) VALUES (?, ?)", (storid, iri))
            return storid

        def _unabbreviate(self, storid):
            iri = _universal_abbrev_2_iri.get(storid)
            if iri is not None:
                return iri
            return self._storid_2_iri.get(storid, str(storid))

        def new_blank_node(self):
            with self._lock:
                self.current_blank += 1
                return -self.current_blank

        def _c_allocate(self, iri):
            """Get or create a context ID for an ontology IRI (from the ontologies table, NOT resources)."""
            row = self._db.execute("SELECT c FROM ontologies WHERE iri=?", (iri,)).fetchone()
            if row:
                return row[0]
            cursor = self._db.execute("INSERT INTO ontologies (iri) VALUES (?)", (iri,))
            self._db.commit()
            return cursor.lastrowid

        def _new_numbered_iri(self, prefix):
            if prefix in self.last_numbered_iri:
                self.last_numbered_iri[prefix] += 1
                return "%s%s" % (prefix, self.last_numbered_iri[prefix])
            i = 1
            while True:
                iri = "%s%d" % (prefix, i)
                if self._abbreviate(iri, create_if_missing=False) is None:
                    self.last_numbered_iri[prefix] = i
                    return iri
                i += 1

        def _refactor(self, storid, new_iri):
            old_iri = self._storid_2_iri.get(storid)
            self._storid_2_iri[storid] = new_iri
            if old_iri and old_iri in self._iri_2_storid:
                del self._iri_2_storid[old_iri]
            self._iri_2_storid[new_iri] = storid
            # Update pyoxigraph store: replace old IRI node with new IRI node in all quads
            if old_iri and "://" in old_iri:
                old_node = pyoxigraph.NamedNode(old_iri)
                new_node = pyoxigraph.NamedNode(new_iri) if "://" in new_iri else pyoxigraph.NamedNode(f"urn:owlready2:{storid}")
                to_add = []
                to_del = list(self._store.quads_for_pattern(old_node, None, None, None))
                to_del += list(self._store.quads_for_pattern(None, None, old_node, None))
                for q in to_del:
                    s = new_node if q.subject    == old_node else q.subject
                    o = new_node if q.object     == old_node else q.object
                    to_add.append(pyoxigraph.Quad(s, q.predicate, o, q.graph_name))
                for q in to_del:
                    self._store.remove(q)
                for q in to_add:
                    self._store.add(q)
            self._db.execute("UPDATE resources SET iri=? WHERE storid=?", (new_iri, storid))

        @property
        def db(self):
            return self._db

        @property
        def c_2_onto(self):
            return self._c_2_onto

        @property
        def _abbreviate_d(self):
            return None

        def restore_iri(self, storid, iri):
            self._db.execute("INSERT INTO resources VALUES (?,?)", (storid, iri))
            self._storid_2_iri[storid] = iri
            self._iri_2_storid[iri]    = storid

        def _destroy_collect_storids(self, destroyed_storids, modified_relations, storid):
            sql = """SELECT s FROM quads WHERE o=? AND p IN (%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d) AND s < 0""" % (
                SOME, ONLY, VALUE,
                owl_onclass, owl_onproperty, owl_complementof,
                owl_inverse_property, owl_ondatarange,
                owl_annotatedsource, owl_annotatedproperty, owl_annotatedtarget,
            )
            for (blank_using,) in list(self._db.execute(sql, (storid,))):
                if blank_using not in destroyed_storids:
                    destroyed_storids.add(blank_using)
                    self._destroy_collect_storids(destroyed_storids, modified_relations, blank_using)

            for (c2, blank_using) in list(self._db.execute(
                    "SELECT c, s FROM objs WHERE o=? AND p=? AND s < 0", (storid, rdf_first))):
                list_user, root, previouss, nexts, length = self._rdf_list_analyze(blank_using)
                destroyed_storids.update(previouss)
                destroyed_storids.add(blank_using)
                destroyed_storids.update(nexts)
                if list_user and list_user not in destroyed_storids:
                    destroyed_storids.add(list_user)
                    self._destroy_collect_storids(destroyed_storids, modified_relations, list_user)

        def _rdf_list_analyze(self, blank):
            previouss = []
            nexts     = []
            length    = 1
            b = self._get_obj_triple_sp_o(blank, rdf_rest)
            while b != rdf_nil:
                nexts.append(b)
                length += 1
                b = self._get_obj_triple_sp_o(b, rdf_rest)

            b = self._get_obj_triple_po_s(rdf_rest, blank)
            if b:
                while b:
                    previouss.append(b)
                    length += 1
                    root = b
                    b    = self._get_obj_triple_po_s(rdf_rest, b)
            else:
                root = blank

            list_user = self._db.execute("SELECT s FROM objs WHERE o=? LIMIT 1", (root,)).fetchone()
            if list_user:
                list_user = list_user[0]
            return list_user, root, previouss, nexts, length

        def destroy_entity(self, storid, destroyer, relation_updater,
                           undoer_objs=None, undoer_datas=None):
            destroyed_storids  = {storid}
            modified_relations = _defaultdict(set)
            self._destroy_collect_storids(destroyed_storids, modified_relations, storid)

            placeholder = ",".join("?" * len(destroyed_storids))
            for s, p in self._db.execute(
                    "SELECT DISTINCT s,p FROM objs WHERE o IN (%s)" % placeholder,
                    tuple(destroyed_storids)):
                if s not in destroyed_storids:
                    modified_relations[s].add(p)

            # Resolve nodes BEFORE cleaning up any state
            nodes_to_remove = [self._storid_to_node(sid) for sid in destroyed_storids]

            for sid in destroyed_storids:
                destroyer(sid)

            for sid, node in zip(destroyed_storids, nodes_to_remove):
                if undoer_objs is not None:
                    undoer_objs .extend(self._db.execute(
                        "SELECT c,s,p,o FROM objs WHERE s=? OR o=?", (sid, sid)))
                    undoer_datas.extend(self._db.execute(
                        "SELECT c,s,p,o,d FROM datas WHERE s=?", (sid,)))
                self._db.execute("DELETE FROM objs  WHERE s=? OR o=?", (sid, sid))
                self._db.execute("DELETE FROM datas WHERE s=?", (sid,))
                for q in list(self._store.quads_for_pattern(node, None, None, None)):
                    self._store.remove(q)
                for q in list(self._store.quads_for_pattern(None, None, node, None)):
                    self._store.remove(q)
            self._db.execute("DELETE FROM resources WHERE storid=?", (storid,))
            self._db.commit()

            # Clean up IRI maps after pyoxigraph operations
            if storid > 0:
                iri = self._storid_2_iri.pop(storid, None)
                if iri and iri in self._iri_2_storid:
                    del self._iri_2_storid[iri]

            for s, ps in modified_relations.items():
                relation_updater(destroyed_storids, s, ps)

            return destroyed_storids

        # -------------------------------------------------------------------
        # Term converters
        # -------------------------------------------------------------------

        def _storid_to_node(self, storid):
            if storid < 0:
                return pyoxigraph.BlankNode(str(-storid))
            iri = self._unabbreviate(storid)
            if "://" not in iri:
                iri = f"urn:owlready2:{storid}"
            return pyoxigraph.NamedNode(iri)

        def _node_to_storid(self, node):
            if isinstance(node, pyoxigraph.BlankNode):
                val = node.value
                try:
                    return -int(val)
                except ValueError:
                    s = self._blank_2_storid.get(val)
                    if s is None:
                        s = self.new_blank_node()
                        self._blank_2_storid[val] = s
                    return s
            if isinstance(node, pyoxigraph.NamedNode):
                return self._abbreviate(node.value)
            return None

        def _val_to_literal(self, value, d):
            """Convert (python_value, datatype_storid) to pyoxigraph.Literal."""
            if d is None or d == 0:
                return pyoxigraph.Literal(str(value))
            if isinstance(d, str) and d.startswith("@"):
                return pyoxigraph.Literal(str(value), language=d[1:])
            dtype_iri = self._unabbreviate(d)
            return pyoxigraph.Literal(str(value), datatype=pyoxigraph.NamedNode(dtype_iri))

        def _literal_to_val(self, lit):
            """Convert pyoxigraph.Literal to (python_value, datatype_storid)."""
            if lit.language:
                return str(lit.value), "@" + lit.language
            if lit.datatype:
                dtype_iri   = lit.datatype.value
                dtype_storid = self._abbreviate(dtype_iri)
                raw          = lit.value
                if dtype_iri in INT_DATATYPES:
                    try:    return int(raw), dtype_storid
                    except: pass
                elif dtype_iri in FLOAT_DATATYPES:
                    try:    return float(raw), dtype_storid
                    except: pass
                return raw, dtype_storid
            return str(lit.value), 0

        # -------------------------------------------------------------------
        # Context helpers
        # -------------------------------------------------------------------

        def context_2_user_context(self, c):
            return self._c_2_onto.get(c)

        def sub_graph(self, onto):
            iri = onto.base_iri
            # Also check alias (when ontology IRI in file differs from request URL)
            already = any(i == iri for _, i in self.ontologies)
            if not already:
                alias_iri = self._ontology_aliases.get(iri)
                if alias_iri:
                    already = any(i == alias_iri for _, i in self.ontologies)
            new_in_quadstore = not already
            sg = OxigraphSubGraph(self, onto)
            self._c_2_onto[sg.c]  = onto
            self.onto_2_subgraph[onto] = sg
            if new_in_quadstore:
                self.ontologies.append((sg.c, iri))
            return sg, new_in_quadstore

        def ontologies_iris(self):
            for _, iri in self.ontologies:
                yield iri

        def fix_base_iri(self, base_iri):
            if base_iri.endswith("#") or base_iri.endswith("/"):
                return base_iri
            prefix_hash  = base_iri + "#"
            prefix_slash = base_iri + "/"
            for iri in self._iri_2_storid:
                if iri.startswith(prefix_hash):
                    return prefix_hash
            for iri in self._iri_2_storid:
                if iri.startswith(prefix_slash):
                    return prefix_slash
            return prefix_hash

        # -------------------------------------------------------------------
        # Infrastructure
        # -------------------------------------------------------------------

        def execute(self, sql, params=()):
            return self._db.execute(sql, params)

        def acquire_write_lock(self): pass
        def release_write_lock(self): pass
        def has_write_lock(self):     return 0
        def commit(self):             self._db.commit()

        def close(self):
            self._db.close()
            self._store = None  # Release pyoxigraph file lock

        def get_fts_prop_storid(self):                 return set()
        def enable_full_text_search(self, storid):     pass
        def disable_full_text_search(self, storid):    pass

        def __bool__(self): return True
        def __len__(self):
            return sum(1 for _ in self._store.quads_for_pattern(None, None, None, None))

        # -------------------------------------------------------------------
        # Iteration helpers (for save / dump)
        # -------------------------------------------------------------------

        def _iter_ontology_iri(self, c=None):
            if c is not None:
                row = self._db.execute("SELECT iri FROM ontologies WHERE c=?", (c,)).fetchone()
                return row[0] if row else None
            return self._db.execute("SELECT c, iri FROM ontologies").fetchall()

        def _iter_triples(self, quads=False, sort_by_s=False, c=None):
            # Mirror triplelite: use the quads UNION VIEW so obj+data triples for
            # the same subject are grouped together (required by the RDF/XML serialiser).
            sql  = ""
            if c is not None:   sql += " WHERE c=%s" % c
            if sort_by_s:       sql += " ORDER BY s"
            if quads:
                return self._db.execute("SELECT c,s,p,o,d FROM quads" + sql)
            else:
                return self._db.execute("SELECT s,p,o,d FROM quads" + sql)

        # -------------------------------------------------------------------
        # BASE_METHODS — object triples
        # -------------------------------------------------------------------

        def _get_obj_triples_cspo_cspo(self, c, s, p, o):
            conds, params = [], []
            if c is not None: conds.append("c=?"); params.append(c)
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            where = " AND ".join(conds) if conds else "1"
            return self._db.execute(
                "SELECT DISTINCT c,s,p,o FROM objs WHERE %s" % where, params)

        def _get_obj_triples_spo_spo(self, s=None, p=None, o=None):
            conds, params = [], []
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            where = " AND ".join(conds) if conds else "1"
            return self._db.execute(
                "SELECT DISTINCT s,p,o FROM objs WHERE %s" % where, params)

        def _get_obj_triples_sp_co(self, s, p):
            return self._db.execute(
                "SELECT DISTINCT c,o FROM objs WHERE s=? AND p=?", (s, p))

        def _get_obj_triples_s_po(self, s):
            return self._db.execute(
                "SELECT DISTINCT p,o FROM objs WHERE s=?", (s,))

        def _get_obj_triples_po_s(self, p, o):
            return (s for (s,) in self._db.execute(
                "SELECT DISTINCT s FROM objs WHERE p=? AND o=?", (p, o)))

        def _get_obj_triples_sp_o(self, s, p):
            return (o for (o,) in self._db.execute(
                "SELECT DISTINCT o FROM objs WHERE s=? AND p=?", (s, p)))

        def _get_obj_triple_sp_o(self, s, p):
            row = self._db.execute(
                "SELECT o FROM objs WHERE s=? AND p=? LIMIT 1", (s, p)).fetchone()
            return row[0] if row else None

        def _get_obj_triple_po_s(self, p, o):
            return next(self._get_obj_triples_po_s(p, o), None)

        def _has_obj_triple_spo(self, s=None, p=None, o=None):
            conds, params = [], []
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            where = " AND ".join(conds) if conds else "1"
            return self._db.execute(
                "SELECT 1 FROM objs WHERE %s LIMIT 1" % where, params).fetchone() is not None

        def _del_obj_triple_raw_spo(self, s=None, p=None, o=None):
            s_n = self._storid_to_node(s) if s is not None else None
            p_n = self._storid_to_node(p) if p is not None else None
            o_n = self._storid_to_node(o) if o is not None else None
            to_del = [q for q in self._store.quads_for_pattern(s_n, p_n, o_n, None)
                      if isinstance(q.object, (pyoxigraph.NamedNode, pyoxigraph.BlankNode))]
            for q in to_del:
                self._store.remove(q)
            conds, params = [], []
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            where = " AND ".join(conds) if conds else "1"
            self._db.execute("DELETE FROM objs WHERE %s" % where, params)

        def _get_obj_triples_spi_o(self, s, p, i):
            # Objects of (s, p, ?) UNION subjects of (?, i, s)
            seen = set()
            for o2 in self._get_obj_triples_sp_o(s, p):
                if o2 not in seen:
                    seen.add(o2)
                    yield o2
            for o2 in self._get_obj_triples_po_s(i, s):
                if o2 not in seen:
                    seen.add(o2)
                    yield o2

        def _get_obj_triples_pio_s(self, p, i, o):
            # Subjects of (?, p, o) UNION objects of (o, i, ?)
            seen = set()
            for s2 in self._get_obj_triples_po_s(p, o):
                if s2 not in seen:
                    seen.add(s2)
                    yield s2
            for s2 in self._get_obj_triples_sp_o(o, i):
                if s2 not in seen:
                    seen.add(s2)
                    yield s2

        # -------------------------------------------------------------------
        # BASE_METHODS — data triples
        # -------------------------------------------------------------------

        def _get_data_triples_spod_spod(self, s=None, p=None, o=None, d=None):
            conds, params = [], []
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None:
                conds.append("o=?"); params.append(o)
                if d is not None: conds.append("d=?"); params.append(d)
            where = " AND ".join(conds) if conds else "1"
            return self._db.execute(
                "SELECT s,p,o,d FROM datas WHERE %s" % where, params)

        def _get_data_triples_sp_od(self, s, p):
            return self._db.execute(
                "SELECT o,d FROM datas WHERE s=? AND p=?", (s, p))

        def _get_data_triple_sp_od(self, s, p):
            return self._db.execute(
                "SELECT o,d FROM datas WHERE s=? AND p=? LIMIT 1", (s, p)).fetchone()

        def _get_data_triples_s_pod(self, s):
            return self._db.execute("SELECT p,o,d FROM datas WHERE s=?", (s,)).fetchall()

        def _has_data_triple_spod(self, s=None, p=None, o=None, d=None):
            conds, params = [], []
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            if d is not None: conds.append("d=?"); params.append(d)
            where = " AND ".join(conds) if conds else "1"
            return self._db.execute(
                "SELECT 1 FROM datas WHERE %s LIMIT 1" % where, params).fetchone() is not None

        def _del_data_triple_raw_spod(self, s=None, p=None, o=None, d=None):
            s_n = self._storid_to_node(s) if s is not None else None
            p_n = self._storid_to_node(p) if p is not None else None
            to_del = []
            for quad in self._store.quads_for_pattern(s_n, p_n, None, None):
                if not isinstance(quad.object, pyoxigraph.Literal):
                    continue
                if o is not None:
                    o2, d2 = self._literal_to_val(quad.object)
                    if o2 != o:
                        continue
                    if d is not None and d2 != d:
                        continue
                to_del.append(quad)
            for q in to_del:
                self._store.remove(q)
            conds, params = [], []
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            if d is not None: conds.append("d=?"); params.append(d)
            where = " AND ".join(conds) if conds else "1"
            self._db.execute("DELETE FROM datas WHERE %s" % where, params)

        # -------------------------------------------------------------------
        # BASE_METHODS — mixed (quads view)
        # -------------------------------------------------------------------

        def _get_triples_spod_spod(self, s=None, p=None, o=None, d=None):
            # Mirror triplelite's exact branching: d is only used when o is also given
            conds, params = [], []
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None:
                conds.append("o=?"); params.append(o)
                if d is not None: conds.append("d=?"); params.append(d)
            where = " AND ".join(conds) if conds else "1"
            return self._db.execute(
                "SELECT s,p,o,d FROM quads WHERE %s" % where, params)

        def _get_triples_sp_od(self, s, p):
            return self._db.execute(
                "SELECT o,d FROM quads WHERE s=? AND p=?", (s, p))

        def _get_triple_sp_od(self, s, p):
            return self._db.execute(
                "SELECT o,d FROM quads WHERE s=? AND p=? LIMIT 1", (s, p)).fetchone()

        def _get_triples_s_pod(self, s):
            return self._db.execute("SELECT p,o,d FROM quads WHERE s=?", (s,)).fetchall()

        def _get_triples_s_p(self, s):
            return (p for (p,) in self._db.execute(
                "SELECT DISTINCT p FROM quads WHERE s=?", (s,)))

        def _get_obj_triples_o_p(self, o):
            return (p for (p,) in self._db.execute(
                "SELECT DISTINCT p FROM objs WHERE o=?", (o,)))


    # -----------------------------------------------------------------------
    # Per-ontology subgraph
    # -----------------------------------------------------------------------

    class OxigraphSubGraph(BaseSubGraph):

        def __init__(self, parent, onto):
            BaseSubGraph.__init__(self, parent, onto)
            onto_iri = onto.base_iri
            # c comes from the dedicated ontologies table (like triplelite), NOT resources
            self.c   = parent._c_allocate(onto_iri)
            # pyoxigraph requires an absolute IRI for named graphs;
            # synthesize one when the ontology IRI has no scheme.
            graph_iri = onto_iri if "://" in onto_iri else f"urn:owlready2:{self.c}"
            self._graph = pyoxigraph.NamedNode(graph_iri)
            parent._c_2_graphname[self.c]     = self._graph
            parent._graphname_to_c[graph_iri] = self.c

        # -- parse callback factory (called by BaseSubGraph.parse()) --------

        def create_parse_func(self, filename=None, delete_existing_triples=True,
                               datatype_attr="http://www.w3.org/1999/02/22-rdf-syntax-ns#datatype"):
            parent    = self.parent
            store     = parent._store
            g         = self._graph
            objs      = []
            datas     = []
            c_storid  = self.c
            db        = parent._db

            if delete_existing_triples:
                for q in list(store.quads_for_pattern(None, None, None, g)):
                    store.remove(q)
                db.execute("DELETE FROM objs  WHERE c=?", (c_storid,))
                db.execute("DELETE FROM datas WHERE c=?", (c_storid,))

            _abbreviate = parent._abbreviate

            def insert_objs():
                db.executemany(
                    "INSERT OR IGNORE INTO objs (c,s,p,o) VALUES (?,?,?,?)",
                    [(c_storid, s2, p2, o2) for s2, p2, o2 in objs])
                for s2, p2, o2 in objs:
                    store.add(pyoxigraph.Quad(
                        parent._storid_to_node(s2),
                        parent._storid_to_node(p2),
                        parent._storid_to_node(o2),
                        g))
                objs.clear()

            def insert_datas():
                db.executemany(
                    "INSERT OR IGNORE INTO datas (c,s,p,o,d) VALUES (?,?,?,?,?)",
                    [(c_storid, s2, p2, o2, d2) for s2, p2, o2, d2 in datas])
                for s2, p2, o2, d2 in datas:
                    store.add(pyoxigraph.Quad(
                        parent._storid_to_node(s2),
                        parent._storid_to_node(p2),
                        parent._val_to_literal(o2, d2),
                        g))
                datas.clear()

            def on_prepare_obj(s, p, o):
                if isinstance(s, str): s = _abbreviate(s)
                if isinstance(o, str): o = _abbreviate(o)
                objs.append((s, _abbreviate(p), o))
                if len(objs) > 1_000_000:
                    insert_objs()

            def on_prepare_data(s, p, o, d):
                if isinstance(s, str): s = _abbreviate(s)
                p2 = _abbreviate(p)
                if d and not d.startswith("@"):
                    d = _abbreviate(d)
                elif not d:
                    d = 0
                datas.append((s, p2, o, d))
                if len(datas) > 1_000_000:
                    insert_datas()

            def on_finish():
                insert_objs()
                insert_datas()
                db.commit()

                # Discover the ontology base IRI from the triples
                s_rdf_type    = parent._storid_to_node(rdf_type)
                s_owl_onto    = parent._storid_to_node(owl_ontology)
                onto_base_iri = ""
                for quad in store.quads_for_pattern(None, s_rdf_type, s_owl_onto, g):
                    iri = parent._unabbreviate(parent._node_to_storid(quad.subject))
                    if iri:
                        onto_base_iri = iri
                        break

                if onto_base_iri and not onto_base_iri.endswith("/"):
                    onto_base_iri = parent.fix_base_iri(onto_base_iri)

                return onto_base_iri

            return (objs, datas, on_prepare_obj, on_prepare_data,
                    insert_objs, insert_datas, parent.new_blank_node,
                    _abbreviate, on_finish)

        # -- ONTO_METHODS — writes to THIS named graph ----------------------

        def _add_obj_triple_raw_spo(self, s, p, o):
            if s is None or p is None or o is None:
                raise ValueError
            self.parent._store.add(pyoxigraph.Quad(
                self.parent._storid_to_node(s),
                self.parent._storid_to_node(p),
                self.parent._storid_to_node(o),
                self._graph))
            self.parent._db.execute(
                "INSERT OR IGNORE INTO objs (c,s,p,o) VALUES (?,?,?,?)",
                (self.c, s, p, o))

        def _set_obj_triple_raw_spo(self, s, p, o):
            if s is None or p is None or o is None:
                raise ValueError
            s_n = self.parent._storid_to_node(s)
            p_n = self.parent._storid_to_node(p)
            for q in list(self.parent._store.quads_for_pattern(s_n, p_n, None, self._graph)):
                if isinstance(q.object, (pyoxigraph.NamedNode, pyoxigraph.BlankNode)):
                    self.parent._store.remove(q)
            self.parent._store.add(pyoxigraph.Quad(
                s_n, p_n, self.parent._storid_to_node(o), self._graph))
            self.parent._db.execute(
                "DELETE FROM objs WHERE c=? AND s=? AND p=?", (self.c, s, p))
            self.parent._db.execute(
                "INSERT OR IGNORE INTO objs (c,s,p,o) VALUES (?,?,?,?)",
                (self.c, s, p, o))

        def _add_data_triple_raw_spod(self, s, p, o, d):
            if s is None or p is None or o is None or d is None:
                raise ValueError
            self.parent._store.add(pyoxigraph.Quad(
                self.parent._storid_to_node(s),
                self.parent._storid_to_node(p),
                self.parent._val_to_literal(o, d),
                self._graph))
            self.parent._db.execute(
                "INSERT OR IGNORE INTO datas (c,s,p,o,d) VALUES (?,?,?,?,?)",
                (self.c, s, p, o, d))

        def _set_data_triple_raw_spod(self, s, p, o, d):
            if s is None or p is None or o is None or d is None:
                raise ValueError
            s_n = self.parent._storid_to_node(s)
            p_n = self.parent._storid_to_node(p)
            for q in list(self.parent._store.quads_for_pattern(s_n, p_n, None, self._graph)):
                if isinstance(q.object, pyoxigraph.Literal):
                    self.parent._store.remove(q)
            self.parent._store.add(pyoxigraph.Quad(
                s_n, p_n, self.parent._val_to_literal(o, d), self._graph))
            self.parent._db.execute(
                "DELETE FROM datas WHERE c=? AND s=? AND p=?", (self.c, s, p))
            self.parent._db.execute(
                "INSERT OR IGNORE INTO datas (c,s,p,o,d) VALUES (?,?,?,?,?)",
                (self.c, s, p, o, d))

        # -- ONTO_METHODS — deletes scoped to THIS named graph --------------

        def _del_obj_triple_raw_spo(self, s=None, p=None, o=None):
            s_n = self.parent._storid_to_node(s) if s is not None else None
            p_n = self.parent._storid_to_node(p) if p is not None else None
            o_n = self.parent._storid_to_node(o) if o is not None else None
            to_del = [q for q in self.parent._store.quads_for_pattern(s_n, p_n, o_n, self._graph)
                      if isinstance(q.object, (pyoxigraph.NamedNode, pyoxigraph.BlankNode))]
            for q in to_del:
                self.parent._store.remove(q)
            conds, params = ["c=?"], [self.c]
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            self.parent._db.execute("DELETE FROM objs WHERE %s" % " AND ".join(conds), params)

        def _del_data_triple_raw_spod(self, s=None, p=None, o=None, d=None):
            s_n = self.parent._storid_to_node(s) if s is not None else None
            p_n = self.parent._storid_to_node(p) if p is not None else None
            to_del = []
            for quad in self.parent._store.quads_for_pattern(s_n, p_n, None, self._graph):
                if not isinstance(quad.object, pyoxigraph.Literal):
                    continue
                if o is not None:
                    o2, d2 = self.parent._literal_to_val(quad.object)
                    if o2 != o:             continue
                    if d is not None and d2 != d: continue
                to_del.append(quad)
            for q in to_del:
                self.parent._store.remove(q)
            conds, params = ["c=?"], [self.c]
            if s is not None: conds.append("s=?"); params.append(s)
            if p is not None: conds.append("p=?"); params.append(p)
            if o is not None: conds.append("o=?"); params.append(o)
            if d is not None: conds.append("d=?"); params.append(d)
            self.parent._db.execute("DELETE FROM datas WHERE %s" % " AND ".join(conds), params)

        # -- BASE_METHODS — delegate to parent (world-wide reads) ----------

        def _get_obj_triples_cspo_cspo(self, c, s, p, o):
            return self.parent._get_obj_triples_cspo_cspo(c, s, p, o)
        def _get_obj_triples_spo_spo(self, s=None, p=None, o=None):
            return self.parent._get_obj_triples_spo_spo(s, p, o)
        def _get_obj_triples_sp_co(self, s, p):
            return self.parent._get_obj_triples_sp_co(s, p)
        def _get_obj_triples_s_po(self, s):
            return self.parent._get_obj_triples_s_po(s)
        def _get_obj_triples_po_s(self, p, o):
            return self.parent._get_obj_triples_po_s(p, o)
        def _get_obj_triples_sp_o(self, s, p):
            return self.parent._get_obj_triples_sp_o(s, p)
        def _get_obj_triple_sp_o(self, s, p):
            return self.parent._get_obj_triple_sp_o(s, p)
        def _get_obj_triple_po_s(self, p, o):
            return self.parent._get_obj_triple_po_s(p, o)
        def _has_obj_triple_spo(self, s=None, p=None, o=None):
            return self.parent._has_obj_triple_spo(s, p, o)
        def _get_obj_triples_spi_o(self, s, p, i):
            return self.parent._get_obj_triples_spi_o(s, p, i)
        def _get_obj_triples_pio_s(self, p, i, o):
            return self.parent._get_obj_triples_pio_s(p, i, o)

        def _get_data_triples_spod_spod(self, s=None, p=None, o=None, d=None):
            return self.parent._get_data_triples_spod_spod(s, p, o, d)
        def _get_data_triples_sp_od(self, s, p):
            return self.parent._get_data_triples_sp_od(s, p)
        def _get_data_triple_sp_od(self, s, p):
            return self.parent._get_data_triple_sp_od(s, p)
        def _get_data_triples_s_pod(self, s):
            return self.parent._get_data_triples_s_pod(s)
        def _has_data_triple_spod(self, s=None, p=None, o=None, d=None):
            return self.parent._has_data_triple_spod(s, p, o, d)

        def _get_triples_spod_spod(self, s=None, p=None, o=None, d=None):
            return self.parent._get_triples_spod_spod(s, p, o, d)
        def _get_triples_sp_od(self, s, p):
            return self.parent._get_triples_sp_od(s, p)
        def _get_triple_sp_od(self, s, p):
            return self.parent._get_triple_sp_od(s, p)
        def _get_triples_s_pod(self, s):
            return self.parent._get_triples_s_pod(s)
        def _get_triples_s_p(self, s):
            return self.parent._get_triples_s_p(s)
        def _get_obj_triples_o_p(self, o):
            return self.parent._get_obj_triples_o_p(o)

        def _iter_triples(self, quads=False, sort_by_s=False, c=None):
            return self.parent._iter_triples(quads=quads, sort_by_s=sort_by_s, c=self.c)

        # Context-scoped query overrides (mirror triplelite SubGraph behaviour)

        def _get_obj_triples_sp_o(self, s, p):
            return (o for (o,) in self.parent._db.execute(
                "SELECT o FROM objs WHERE c=? AND s=? AND p=?", (self.c, s, p)))

        def _get_obj_triple_sp_o(self, s, p):
            row = self.parent._db.execute(
                "SELECT o FROM objs WHERE c=? AND s=? AND p=? LIMIT 1", (self.c, s, p)).fetchone()
            return row[0] if row else None

        def _get_obj_triples_s_po(self, s):
            return self.parent._db.execute(
                "SELECT p,o FROM objs WHERE c=? AND s=?", (self.c, s)).fetchall()

        def _get_obj_triples_sp_co(self, s, p):
            return self.parent._db.execute(
                "SELECT c,o FROM objs WHERE c=? AND s=? AND p=?", (self.c, s, p)).fetchall()

        def _get_obj_triples_po_s(self, p, o):
            return (s for (s,) in self.parent._db.execute(
                "SELECT s FROM objs WHERE c=? AND p=? AND o=?", (self.c, p, o)))

        def _get_obj_triple_po_s(self, p, o):
            row = self.parent._db.execute(
                "SELECT s FROM objs WHERE c=? AND p=? AND o=? LIMIT 1", (self.c, p, o)).fetchone()
            return row[0] if row else None

        def _get_obj_triples_spi_o(self, s, p, i):
            return (x for (x,) in self.parent._db.execute(
                "SELECT o FROM objs WHERE c=? AND s=? AND p=? "
                "UNION SELECT s FROM objs WHERE c=? AND p=? AND o=?",
                (self.c, s, p, self.c, i, s)))

        def _get_obj_triples_pio_s(self, p, i, o):
            return (x for (x,) in self.parent._db.execute(
                "SELECT s FROM objs WHERE c=? AND p=? AND o=? "
                "UNION SELECT o FROM objs WHERE c=? AND s=? AND p=?",
                (self.c, p, o, self.c, o, i)))

        def _get_triples_sp_od(self, s, p):
            return self.parent._db.execute(
                "SELECT o,d FROM quads WHERE c=? AND s=? AND p=?", (self.c, s, p))

        def _get_triple_sp_od(self, s, p):
            return self.parent._db.execute(
                "SELECT o,d FROM quads WHERE c=? AND s=? AND p=? LIMIT 1", (self.c, s, p)).fetchone()

        def _get_data_triples_sp_od(self, s, p):
            return self.parent._db.execute(
                "SELECT o,d FROM datas WHERE c=? AND s=? AND p=?", (self.c, s, p))

        def _get_data_triple_sp_od(self, s, p):
            return self.parent._db.execute(
                "SELECT o,d FROM datas WHERE c=? AND s=? AND p=? LIMIT 1", (self.c, s, p)).fetchone()

        def _get_data_triples_s_pod(self, s):
            return self.parent._db.execute(
                "SELECT p,o,d FROM datas WHERE c=? AND s=?", (self.c, s)).fetchall()

        def _get_triples_s_pod(self, s):
            return self.parent._db.execute(
                "SELECT p,o,d FROM quads WHERE c=? AND s=?", (self.c, s)).fetchall()

        def _get_obj_triples_transitive_sp(self, s, p, already=None):
            return self.parent._get_obj_triples_transitive_sp(s, p, already)
        def _get_obj_triples_transitive_po(self, p, o, already=None):
            return self.parent._get_obj_triples_transitive_po(p, o, already)
        def _get_obj_triples_transitive_sym(self, s, p, already=None):
            return self.parent._get_obj_triples_transitive_sym(s, p, already)
        def _get_obj_triples_transitive_sp_indirect(self, s, predicates_inverses, already=None):
            return self.parent._get_obj_triples_transitive_sp_indirect(s, predicates_inverses, already)

        # -- Misc -----------------------------------------------------------

        def context_2_user_context(self, c):
            return self.parent.context_2_user_context(c)

        def _abbreviate(self, iri, create_if_missing=True):
            return self.parent._abbreviate(iri, create_if_missing)

        def _unabbreviate(self, storid):
            return self.parent._unabbreviate(storid)

        def _refactor(self, storid, new_iri):
            return self.parent._refactor(storid, new_iri)

        def _new_numbered_iri(self, prefix):
            return self.parent._new_numbered_iri(prefix)

        def new_blank_node(self):
            return self.parent.new_blank_node()

        def acquire_write_lock(self): pass
        def release_write_lock(self): pass
        def commit(self): pass

        def set_last_update_time(self, t):
            self.parent._last_update_time[self.c] = t

        def get_last_update_time(self):
            return self.parent._last_update_time.get(self.c, 0.0)

        def add_ontology_alias(self, iri, alias):
            self.parent._ontology_aliases[iri] = alias

        def _iter_ontology_iri(self, c=None):
            return self.parent._iter_ontology_iri(c if c is not None else self.c)

        def destroy(self):
            for q in list(self.parent._store.quads_for_pattern(None, None, None, self._graph)):
                self.parent._store.remove(q)
            self.parent._db.execute("DELETE FROM objs WHERE c=?", (self.c,))
            self.parent._db.execute("DELETE FROM datas WHERE c=?", (self.c,))
            self.parent._c_2_graphname.pop(self.c, None)
            self.parent._graphname_to_c.pop(self.onto.base_iri, None)
            self.parent.ontologies = [(c, i) for c, i in self.parent.ontologies if c != self.c]
            self.parent.onto_2_subgraph.pop(self.onto, None)

        def __len__(self):
            return sum(1 for _ in self.parent._store.quads_for_pattern(
                None, None, None, self._graph))

        def __bool__(self): return True
