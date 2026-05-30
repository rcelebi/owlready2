"""Owlready2 Explorer — Streamlit UI.

Run:
    streamlit run owlready2/ui_explorer.py

Query path summary (see README.md for full details):

  Path 1 — Raw SPARQL (no conversion)
    Used by: SPARQL tab, DL Query restrictions, transitive descendants
    How: ox_store.query(sparql) → raw pyoxigraph NamedNode / Literal terms
    Cost: SPARQL parse + plan + scan; no Python object construction

  Path 2 — owlready2 Python API (SQL)
    Used by: Info tab, DL Query direct subclasses
    How: .classes() / .subclasses() / .properties() → SQLite recursive CTE
    Cost: SQL queries; fast for direct lookups, slow for deep transitive traversal

  Path 3 — SPARQL + conversion (owlready2 objects)
    Used by: display-time only, for ≤ _DISPLAY_CAP rows shown in the table
    How: world.get(iri) per displayed row
    Cost: per-row IRI→entity resolution; acceptable because count is bounded

Store resolution (_resolve_sparql_store):
  1. Pre-built persistent pyoxigraph store (OWL_NT_FILE.ox_store or /tmp/snomed_ox_store)
  2. tripleoxigraph backend → world.graph._store (always current, no rebuild needed)
  3. triplelite backend → in-memory store built from SQLite (one-time, cached)
"""

import sys, os, re as _re, subprocess, tempfile, time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st

st.set_page_config(page_title="Owlready2 Explorer", page_icon="🦉", layout="wide")

try:
    import owlready2 as owl
    from owlready2.manchester import parse_manchester_expression, instances_of, to_manchester
    owl.set_log_level(0)
except Exception as e:
    st.error(f"Cannot import owlready2: {e}")
    st.stop()

# ── constants ─────────────────────────────────────────────────────────────────
_SPARQL_ROW_CAP = 10_000   # max rows streamed from ox_store.query() before truncation
_DISPLAY_CAP    = 1_000    # max rows resolved to owlready2 objects for display

_RDF_TYPE  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_RDFS_SUB  = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
_SCT_BASE  = "http://snomed.info/id/"

# ── session state defaults ────────────────────────────────────────────────────
for k, v in {
    "selected_path":   "",
    "last_browse_dir": os.path.expanduser("~"),
    "world":           None,
    "onto":            None,
    "reasoned":        False,
    "tmpfiles":        [],
    # SPARQL store cache — invalidated when a new world is loaded
    "sparql_store":    None,   # pyoxigraph.Store
    "sparql_union":    False,  # use_default_graph_as_union flag
    "sparql_label":    "",     # human-readable store description
    "sparql_world":    None,   # world the store belongs to
    # DL query result cache — invalidated on new world load
    "dl_cache":        {},     # (parsed_expr, mode, direct) → sorted list[str] of IRIs
}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ══════════════════════════════════════════════════════════════════════════════
# Store resolution
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_sparql_store(world):
    """Return (ox_store, use_union, label), cached per world.

    Resolution order — first match wins:
      1. Pre-built persistent pyoxigraph store (fastest, sub-ms open)
      2. tripleoxigraph backend: world.graph._store — live RocksDB, always current
      3. triplelite backend: build in-memory store from SQLite — expensive, one-time
    """
    import pyoxigraph as _ox

    if st.session_state["sparql_world"] is world and st.session_state["sparql_store"] is not None:
        return (st.session_state["sparql_store"],
                st.session_state["sparql_union"],
                st.session_state["sparql_label"])

    def _cache(store, union, label):
        st.session_state.update(sparql_store=store, sparql_union=union,
                                sparql_label=label, sparql_world=world)
        return store, union, label

    # 1. Pre-built persistent store (triples in default graph → union=False)
    nt = os.environ.get("OWL_NT_FILE", "")
    for store_dir in [nt + ".ox_store" if nt else "", "/tmp/snomed_ox_store"]:
        if store_dir and os.path.exists(store_dir + "/.ready"):
            try:
                return _cache(_ox.Store(store_dir), False, f"persistent store ({store_dir})")
            except Exception:
                pass

    # 2. tripleoxigraph backend (triples in named graphs → union=True)
    if hasattr(world.graph, "_store"):
        return _cache(world.graph._store, True, "tripleoxigraph RocksDB store")

    # 3. triplelite: build in-memory store (shown once per ontology load)
    with st.spinner("Building in-memory SPARQL store from SQLite (one-time) …"):
        store = world.as_sparql_graph()._get_cached_store()
    return _cache(store, False, "in-memory store (built from SQLite)")


# ══════════════════════════════════════════════════════════════════════════════
# DL evaluation — Path 1 (SPARQL) + Path 2 (SQL), returns IRI strings
# ══════════════════════════════════════════════════════════════════════════════

def _eval_dl_to_iris(expr, world, ox_store, use_union, direct):
    """Evaluate a Manchester expression; return a set of matching class IRI strings.

    Works entirely with IRI strings — no owlready2 object creation for
    intermediate results.  Callers resolve to objects only for the display slice.

    Named class, direct=True  → Path 2: .subclasses() SQL — sub-ms
    Named class, direct=False → Path 1: rdfs:subClassOf+ SPARQL path — ~250 ms
                                (vs 3–4 s for the SQL recursive CTE at SNOMED scale)
    Restriction               → Path 1: SPARQL on pyoxigraph — IRI strings directly,
                                no per-row world.get() calls
    And / Or / Not            → set intersection / union / difference on IRI strings
    Cardinality               → Counter on subject IRI strings
    """
    import pyoxigraph as _ox
    from owlready2.class_construct import Restriction, And, Or, Not
    from owlready2.base import SOME, VALUE, MIN, MAX, EXACTLY
    from collections import Counter as _Counter

    def _sparql(q):
        return ox_store.query(q, use_default_graph_as_union=use_union)

    def _named_nodes(rows):
        return {r[0].value for r in rows if isinstance(r[0], _ox.NamedNode)}

    # Named OWL class
    if isinstance(expr, type):
        if direct:
            # Path 2: SQL direct subclasses — always fast
            return {c.iri for c in expr.subclasses()}
        if ox_store is not None:
            # Path 1: SPARQL property path for transitive closure
            return _named_nodes(_sparql(
                f"SELECT ?s WHERE {{ ?s <{_RDFS_SUB}>+ <{expr.iri}> }}"
            ))
        # Fallback: SQL transitive (slow for large hierarchies)
        return {c.iri for c in expr.descendants()}

    if isinstance(expr, And):
        pos = [e for e in expr.Classes if not isinstance(e, Not)]
        neg = [e.Class for e in expr.Classes if isinstance(e, Not)]
        result = (
            _eval_dl_to_iris(pos[0], world, ox_store, use_union, direct)
            if pos else {c.iri for c in world.classes()}
        )
        for e in pos[1:]:
            result &= _eval_dl_to_iris(e, world, ox_store, use_union, direct)
        for e in neg:
            result -= _eval_dl_to_iris(e, world, ox_store, use_union, direct)
        return result

    if isinstance(expr, Or):
        result = set()
        for e in expr.Classes:
            result |= _eval_dl_to_iris(e, world, ox_store, use_union, direct)
        return result

    if isinstance(expr, Not):
        return ({c.iri for c in world.classes()}
                - _eval_dl_to_iris(expr.Class, world, ox_store, use_union, direct))

    if isinstance(expr, Restriction):
        if ox_store is None:
            raise RuntimeError("Role restrictions require the pyoxigraph store.")
        prop_iri = expr.property.iri
        rtype    = expr.type

        if rtype == VALUE:
            return _named_nodes(_sparql(
                f"SELECT DISTINCT ?s WHERE {{ ?s <{prop_iri}> <{expr.value.iri}> }}"
            ))

        if rtype == SOME:
            filler = expr.value
            if filler is owl.Thing or not isinstance(filler, type):
                q = f"SELECT DISTINCT ?s WHERE {{ ?s <{prop_iri}> ?v }}"
            else:
                q = f"SELECT DISTINCT ?s WHERE {{ ?s <{prop_iri}> <{filler.iri}> }}"
            return _named_nodes(_sparql(q))

        if rtype in (MIN, MAX, EXACTLY):
            n      = expr.cardinality
            counts = _Counter(
                r[0].value for r in _sparql(f"SELECT ?s ?v WHERE {{ ?s <{prop_iri}> ?v }}")
                if isinstance(r[0], _ox.NamedNode)
            )
            if rtype == MIN:   return {iri for iri, c in counts.items() if c >= n}
            if rtype == MAX:   return {iri for iri, c in counts.items() if c <= n}
            return             {iri for iri, c in counts.items() if c == n}

        raise NotImplementedError(f"Restriction type {rtype} not supported")

    raise NotImplementedError(f"Cannot evaluate {type(expr).__name__}")


def _iris_to_rows(iris, world, limit=_DISPLAY_CAP):
    """Resolve a sorted IRI list to display dicts — Path 3, bounded by limit."""
    rows = []
    for iri in iris[:limit]:
        h = world.get(iri)
        rows.append({
            "Class": getattr(h, "name", iri.split("#")[-1].split("/")[-1]),
            "Label": str(next(iter(h.label), "")) if h and getattr(h, "label", None) else "",
            "IRI":   iri,
        })
    return rows


# ── SNOMED IRI expansion ──────────────────────────────────────────────────────
def _expand_snomed_iris(text):
    text = _re.sub(r'\bsct:(\d+)\b',
                   lambda m: f'<{_SCT_BASE}{m.group(1)}>', text)
    text = _re.sub(r'(?<![:\w<])(\d{6,18})(?![\w>])',
                   lambda m: f'<{_SCT_BASE}{m.group(1)}>', text)
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Auto-load from environment variables
# ══════════════════════════════════════════════════════════════════════════════

_sqlite_cache = os.environ.get("OWL_SQLITE_CACHE", "")
_nt_file      = os.environ.get("OWL_NT_FILE", "")

if _sqlite_cache and st.session_state["onto"] is None and os.path.isfile(_sqlite_cache):
    with st.spinner(f"Loading cached world from {_sqlite_cache} …"):
        try:
            w = owl.World()
            w.set_backend(filename=_sqlite_cache)
            if w.graph.execute("SELECT COUNT(*) FROM objs").fetchone()[0] > 0:
                onto_auto = next(iter(w.ontologies.values()), None) or w.get_ontology("http://auto-loaded/")
                st.session_state["world"] = w
                st.session_state["onto"]  = onto_auto
        except Exception as _e:
            st.warning(f"Auto-load failed: {_e}")

if _nt_file and st.session_state["onto"] is None and os.path.isfile(_nt_file):
    _sidecar = _nt_file + ".world.sqlite3"
    _msg = ("Opening sidecar cache …" if os.path.isfile(_sidecar)
            else f"Parsing {os.path.basename(_nt_file)} and building sidecar cache …")
    with st.spinner(_msg):
        try:
            w = owl.World()
            w.set_backend(filename=_sidecar)
            if w.graph.execute("SELECT COUNT(*) FROM objs").fetchone()[0] > 100_000:
                onto_auto = next(iter(w.ontologies.values()), None) or w.get_ontology("http://auto-loaded/")
            else:
                with open(_nt_file, "rb") as _fobj:
                    onto_auto = w.get_ontology("http://auto-loaded/").load(fileobj=_fobj, format="ntriples")
                w.graph.commit()
            st.session_state["world"] = w
            st.session_state["onto"]  = onto_auto
        except Exception as _e:
            st.warning(f"NT auto-load failed: {_e}")


# ══════════════════════════════════════════════════════════════════════════════
# Load helpers
# ══════════════════════════════════════════════════════════════════════════════

def _new_world():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    st.session_state["tmpfiles"].append(path)
    w = owl.World()
    w.set_backend(filename=path)
    return w

def _load_nt_fast(path):
    """Load NT via sidecar SQLite cache; returns (world, onto, from_cache)."""
    cache = path + ".world.sqlite3"
    w = owl.World()
    w.set_backend(filename=cache)
    if w.graph.execute("SELECT COUNT(*) FROM objs").fetchone()[0] > 100_000:
        onto = next(iter(w.ontologies.values()), None) or w.get_ontology("http://auto-loaded/")
        return w, onto, True
    with open(path, "rb") as fobj:
        onto = w.get_ontology("http://auto-loaded/" + os.path.basename(path) + "#").load(
            fileobj=fobj, format="ntriples")
    w.graph.commit()
    return w, onto, False

def _reset_caches():
    st.session_state.update(sparql_store=None, sparql_union=False,
                            sparql_label="", sparql_world=None, dl_cache={})


# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════

st.title("🦉 Owlready2 Explorer")
st.divider()

# ── Step 1: Select & Load ─────────────────────────────────────────────────────
st.subheader("Step 1 — Select ontology")
col_browse, col_path, col_load = st.columns([1, 5, 1])

with col_browse:
    if st.button("📂 Browse", use_container_width=True):
        result = subprocess.run(
            ["osascript",
             "-e", "tell application \"System Events\" to activate",
             "-e", (f"POSIX path of (choose file with prompt \"Select OWL file\" "
                    f"default location POSIX file \"{st.session_state['last_browse_dir']}\" "
                    f"of type {{\"owl\",\"rdf\",\"ttl\",\"omn\",\"ofn\",\"xml\",\"nt\",\"n3\"}})")],
            capture_output=True, text=True)
        if result.returncode == 0:
            chosen = result.stdout.strip()
            st.session_state["selected_path"]   = chosen
            st.session_state["path_display"]    = chosen
            st.session_state["last_browse_dir"] = os.path.dirname(chosen)
            st.rerun()

with col_path:
    st.text_input("path", label_visibility="collapsed",
                  placeholder="Click Browse or paste a file path here…",
                  key="path_display")
    st.session_state["selected_path"] = st.session_state.get("path_display", "")

with col_load:
    do_load = st.button("Load", use_container_width=True, type="primary",
                        disabled=not st.session_state["selected_path"].strip())

if do_load:
    path = st.session_state["selected_path"].strip()
    if not os.path.isfile(path):
        st.error(f"File not found: {path}")
    else:
        with st.spinner(f"Loading {os.path.basename(path)} …"):
            try:
                if path.lower().endswith((".nt", ".ntriples")):
                    w, onto, from_cache = _load_nt_fast(path)
                    st.success("Loaded from sidecar cache — instant ⚡" if from_cache
                               else f"Parsed and cached → {path}.world.sqlite3")
                else:
                    file_dir = os.path.dirname(os.path.abspath(path))
                    if file_dir not in owl.onto_path:
                        owl.onto_path.append(file_dir)
                    w    = _new_world()
                    onto = w.get_ontology(f"file://{os.path.abspath(path)}").load()
                st.session_state.update(world=w, onto=onto, reasoned=False)
                _reset_caches()
            except Exception as e:
                st.error(f"Load failed: {e}")
                st.session_state["onto"] = None
        if st.session_state["onto"]:
            st.rerun()

if st.session_state["onto"] is None:
    st.stop()

onto  = st.session_state["onto"]
world = st.session_state["world"]
st.divider()

# ── Summary bar ───────────────────────────────────────────────────────────────
if "onto_counts" not in st.session_state or st.session_state.get("onto_counts_for") is not onto:
    _n_cls  = world.graph.execute("SELECT COUNT(DISTINCT s) FROM objs WHERE p=6").fetchone()[0]
    _n_ind  = world.graph.execute("SELECT COUNT(DISTINCT s) FROM objs WHERE p=7").fetchone()[0]
    _n_prop = sum(1 for _ in onto.properties())
    st.session_state["onto_counts"]     = (_n_cls, _n_ind, _n_prop)
    st.session_state["onto_counts_for"] = onto
_n_cls, _n_ind, _n_prop = st.session_state["onto_counts"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Classes",     f"{_n_cls:,}")
c2.metric("Individuals", f"{_n_ind:,}")
c3.metric("Properties",  f"{_n_prop:,}")
c4.metric("Reasoned",    "✓ yes" if st.session_state["reasoned"] else "✗ no")

with st.expander(f"Loaded: `{onto.base_iri}`", expanded=False):
    col_unload, _ = st.columns([1, 5])
    if col_unload.button("Unload ontology", type="secondary"):
        st.session_state.update(world=None, onto=None, reasoned=False, selected_path="")
        _reset_caches()
        st.rerun()

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_info, tab_reason, tab_axiom, tab_query, tab_sparql = st.tabs([
    "📋 Info", "⚙️ Reasoning", "✏️ Add Axiom", "🔍 DL Query", "🗄️ SPARQL"
])


# ══ Tab: Info ═════════════════════════════════════════════════════════════════
with tab_info:
    _PAGE = 200
    st.markdown("**Classes**")
    i_col1, i_col2 = st.columns([3, 1])
    with i_col2:
        cls_page = st.number_input("Page", min_value=1, key="cls_page", step=1,
                                   label_visibility="collapsed")
    with i_col1:
        cls_filter = st.text_input("Filter by name", placeholder="fracture…",
                                   key="cls_filter", label_visibility="collapsed")
    _all_cls = list(onto.classes())
    if cls_filter.strip():
        _all_cls = [c for c in _all_cls if cls_filter.lower() in (c.name or "").lower()]
    _all_cls.sort(key=lambda c: c.name or "")
    _slice = _all_cls[_PAGE*(cls_page-1): _PAGE*cls_page]
    st.caption(f"Showing {len(_slice)} of {len(_all_cls)} classes (page {cls_page})")
    if _slice:
        st.dataframe(
            [{"Class": c.name, "IRI": c.iri,
              "SubClassOf": ", ".join(p.name for p in c.is_a
                                     if isinstance(p, type) and p is not owl.Thing)}
             for c in _slice],
            use_container_width=True, hide_index=True)

    st.markdown("**Properties**")
    props     = list(onto.properties())
    obj_props  = [p for p in props if issubclass(type(p), owl.ObjectProperty)]
    data_props = [p for p in props if issubclass(type(p), owl.DataProperty)]
    pcol1, pcol2 = st.columns(2)
    with pcol1:
        st.markdown("*Object properties*")
        for p in obj_props:  st.markdown(f"- `{p.name}`")
    with pcol2:
        st.markdown("*Data properties*")
        for p in data_props: st.markdown(f"- `{p.name}`")

    inds = list(onto.individuals())
    if inds:
        st.markdown("**Individuals**")
        st.dataframe(
            [{"Individual": i.name, "IRI": i.iri,
              "Types": ", ".join(t.name for t in i.is_a if isinstance(t, type))}
             for i in sorted(inds, key=lambda i: i.name or "")[:_PAGE]],
            use_container_width=True, hide_index=True)


# ══ Tab: Reasoning ════════════════════════════════════════════════════════════
with tab_reason:
    r_col1, r_col2, _ = st.columns([2, 2, 4])
    with r_col1:
        reasoner = st.selectbox("Reasoner", ["Pellet", "HermiT"])
    with r_col2:
        infer_props = st.checkbox("Infer property values", value=True)

    if st.button("▶ Run Reasoner", type="primary"):
        with st.spinner(f"Running {reasoner} …"):
            try:
                t0 = time.time()
                with onto:
                    if reasoner == "Pellet":
                        owl.sync_reasoner_pellet(onto, infer_property_values=infer_props)
                    else:
                        owl.sync_reasoner_hermit(onto, infer_property_values=infer_props)
                st.session_state["reasoned"] = True
                st.success(f"{reasoner} finished in {time.time()-t0:.2f}s")
                st.rerun()
            except Exception as e:
                st.error(f"Reasoning failed: {e}")

    if st.session_state["reasoned"]:
        st.info("Reasoning complete. DL queries now reflect inferred classifications.")


# ══ Tab: Add Axiom ════════════════════════════════════════════════════════════
with tab_axiom:
    axiom_mode = st.radio("Axiom type",
                          ["SubClassOf", "EquivalentTo", "Individual type assertion"],
                          horizontal=True)

    if axiom_mode in ("SubClassOf", "EquivalentTo"):
        a_col1, a_col2 = st.columns([1, 2])
        with a_col1:
            class_name = st.text_input("Class name", placeholder="e.g. Pizza")
        with a_col2:
            axiom_expr = st.text_input("Manchester expression",
                                       placeholder="e.g. hasTopping some VegetableTopping")
        if st.button("Add Axiom", type="primary"):
            try:
                cls_obj = world[onto.base_iri + class_name]
                if cls_obj is None:
                    raise ValueError(f"Class '{class_name}' not found.")
                expr = parse_manchester_expression(axiom_expr.strip(), onto)
                with onto:
                    if axiom_mode == "SubClassOf":
                        cls_obj.is_a.append(expr)
                    else:
                        cls_obj.equivalent_to.append(expr)
                st.success(f"{axiom_mode} axiom added to `{class_name}`.")
                st.session_state["reasoned"] = False
                _reset_caches()
            except Exception as e:
                st.error(f"Error: {e}")
    else:
        b_col1, b_col2 = st.columns([1, 2])
        with b_col1:
            ind_name = st.text_input("Individual name", placeholder="e.g. my_pizza")
        with b_col2:
            ind_type = st.text_input("Type (Manchester expression)",
                                     placeholder="e.g. Pizza and (hasTopping some Cheese)")
        if st.button("Add Axiom", type="primary", key="btn_ind"):
            try:
                expr = parse_manchester_expression(ind_type.strip(), onto)
                with onto:
                    existing = world[onto.base_iri + ind_name]
                    if existing is None:
                        owl.Thing(ind_name, namespace=onto).is_a.append(expr)
                    else:
                        existing.is_a.append(expr)
                st.success(f"Type assertion added to `{ind_name}`.")
                st.session_state["reasoned"] = False
                _reset_caches()
            except Exception as e:
                st.error(f"Error: {e}")


# ══ Tab: DL Query ═════════════════════════════════════════════════════════════
with tab_query:
    q_col1, q_col2, q_col3 = st.columns([4, 1, 1])
    with q_col1:
        dl_expr = st.text_input(
            "Manchester class expression",
            placeholder="sct:404684003  or  <http://snomed.info/id/404684003>  or  FindingSite some owl:Thing",
            key="dl_expr")
    with q_col2:
        query_mode = st.selectbox("Return", ["Subclasses", "Individuals"], key="dl_mode")
    with q_col3:
        st.write("")
        direct = st.checkbox("Direct only", key="dl_direct")

    st.caption(
        "Use `sct:XXXXXXX` or a bare SNOMED ID — both expand to full IRIs. "
        "Use `<full IRI>` for non-SNOMED terms. "
        "**Subclasses** returns `owl:Class`; **Individuals** returns `owl:NamedIndividual`.")

    if st.button("▶ Run Query", type="primary", disabled=not dl_expr.strip()):
        try:
            expanded = _expand_snomed_iris(dl_expr.strip())
            expr     = parse_manchester_expression(expanded, onto)
            parsed   = to_manchester(expr)
            st.markdown(f"**Parsed:** `{parsed}`")

            ox_store, use_union, store_label = _resolve_sparql_store(world)
            cache_key = (parsed, query_mode, direct)

            if cache_key in st.session_state["dl_cache"]:
                result_iris = st.session_state["dl_cache"][cache_key]
                time_label  = "(cached)"
            else:
                t0 = time.time()

                if query_mode == "Subclasses":
                    # Path 1 (SPARQL) for transitive, Path 2 (SQL) for direct
                    result_iris = sorted(_eval_dl_to_iris(expr, world, ox_store, use_union, direct))

                else:
                    # Individuals — Path 1: single SPARQL query avoids instances_of()'s
                    # recursive world.search(type=sub) which issues one SQL query per subclass
                    import pyoxigraph as _ox
                    if isinstance(expr, type) and ox_store is not None:
                        if direct:
                            q = f"SELECT DISTINCT ?i WHERE {{ ?i <{_RDF_TYPE}> <{expr.iri}> }}"
                        else:
                            q = (f"SELECT DISTINCT ?i WHERE {{"
                                 f" {{ ?i <{_RDF_TYPE}> <{expr.iri}> }}"
                                 f" UNION"
                                 f" {{ ?sub <{_RDFS_SUB}>+ <{expr.iri}> . ?i <{_RDF_TYPE}> ?sub }}"
                                 f"}}")
                        result_iris = sorted(
                            r[0].value for r in ox_store.query(q, use_default_graph_as_union=use_union)
                            if isinstance(r[0], _ox.NamedNode)
                        )
                    else:
                        # Anonymous expression: fall back to linear scan
                        result_iris = sorted(
                            ind.iri for ind in instances_of(expr, direct=direct, ontology=onto)
                        )

                elapsed     = time.time() - t0
                time_label  = f"in {elapsed:.2f}s"
                st.session_state["dl_cache"][cache_key] = result_iris

            mode_noun = "class(es)" if query_mode == "Subclasses" else "individual(s)"
            st.markdown(f"**{len(result_iris):,} {mode_noun} matched** {time_label}")

            # Path 3: resolve only the displayed slice to owlready2 objects
            rows = _iris_to_rows(result_iris, world)
            if rows:
                st.dataframe(rows, use_container_width=True, hide_index=True)
                if len(result_iris) > _DISPLAY_CAP:
                    st.caption(f"Showing first {_DISPLAY_CAP:,} of {len(result_iris):,}")
            else:
                hint = (" Try **Subclasses** mode — SNOMED uses owl:Class, not owl:NamedIndividual."
                        if query_mode == "Individuals" else "")
                st.info(f"No results.{hint}")

        except NotImplementedError as e:
            st.error(f"Not supported: {e}")
        except Exception as e:
            st.error(f"Error: {e}")

    st.divider()
    st.markdown("##### Subclass / superclass lookup")
    sc_col1, sc_col2, sc_col3 = st.columns([3, 2, 1])
    with sc_col1:
        sc_name = st.text_input("Class name or IRI fragment", placeholder="e.g. ClinicalFinding",
                                key="sc_name")
    with sc_col2:
        sc_dir = st.selectbox("Direction", ["Subclasses", "Superclasses"])
    with sc_col3:
        st.write("")
        do_sc = st.button("Run", key="btn_sc")

    if do_sc and sc_name.strip():
        try:
            _term   = sc_name.strip()
            cls_obj = (world.get(_term)
                       or world.get(onto.base_iri + _term)
                       or next((c for c in onto.classes()
                                if (c.name or "").lower() == _term.lower()
                                or any(_term.lower() in str(l).lower()
                                       for l in (c.label or []))), None))
            if cls_obj is None:
                st.error(f"Class '{_term}' not found.")
            else:
                hits = (list(cls_obj.subclasses()) if sc_dir == "Subclasses"
                        else [a for a in cls_obj.ancestors() if a is not cls_obj])
                st.markdown(f"**{len(hits)} result(s) for `{cls_obj.name}`:**")
                st.dataframe(
                    [{"Class": getattr(h, "name", repr(h)), "IRI": getattr(h, "iri", "")}
                     for h in sorted(hits, key=lambda c: getattr(c, "name", "") or "")[:500]],
                    use_container_width=True, hide_index=True)
                if len(hits) > 500:
                    st.caption(f"Showing first 500 of {len(hits)}")
        except Exception as e:
            st.error(f"Error: {e}")


# ══ Tab: SPARQL ═══════════════════════════════════════════════════════════════
with tab_sparql:
    _default_sparql = (
        "PREFIX owl:  <http://www.w3.org/2002/07/owl#>\n"
        "PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n\n"
        "SELECT ?cls WHERE {\n"
        "  ?cls rdf:type owl:Class .\n"
        "} LIMIT 20"
    )
    sparql_text = st.text_area("SPARQL query", value=_default_sparql, height=180)

    if st.button("▶ Run SPARQL", type="primary", disabled=not sparql_text.strip()):
        try:
            import pyoxigraph as _ox

            # Path 1: raw SPARQL — resolve store once, reuse on subsequent runs
            ox_store, use_union, store_label = _resolve_sparql_store(world)
            st.caption(f"Querying: {store_label}")

            def _cell(v):
                if v is None:                     return ""
                if isinstance(v, _ox.NamedNode):  return v.value
                if isinstance(v, _ox.Literal):    return str(v.value)
                if isinstance(v, _ox.BlankNode):  return f"_:{v.value}"
                return str(v)

            t0, raw, truncated = time.time(), [], False
            for row in ox_store.query(sparql_text, use_default_graph_as_union=use_union):
                raw.append(row)
                if len(raw) >= _SPARQL_ROW_CAP:
                    truncated = True
                    break
            elapsed = time.time() - t0

            count_label = f"{len(raw):,}+" if truncated else f"{len(raw):,}"
            st.markdown(f"**{count_label} row(s)** in {elapsed:.3f}s")
            if truncated:
                st.warning(f"Results capped at {_SPARQL_ROW_CAP:,} rows — add LIMIT to your query.")

            if raw:
                first = raw[0]
                if hasattr(first, "_fields"):
                    cols = list(first._fields)
                    rows = [{c: _cell(getattr(r, c)) for c in cols} for r in raw]
                else:
                    cols = [f"col{i}" for i in range(len(first))]
                    rows = [{c: _cell(r[i]) for i, c in enumerate(cols)} for r in raw]
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.info("No results.")
        except Exception as e:
            st.error(f"Error: {e}")
