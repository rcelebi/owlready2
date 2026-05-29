# -*- coding: utf-8 -*-
"""OWL Manchester Syntax support for owlready2.

Public API
----------
Serialiser:
  to_manchester(expr, prefixes=None)          -> str
  manchester_render_ontology(onto, **kw)      -> str

Parser:
  parse_manchester_expression(text, onto)     -> owlready2 construct
  parse_manchester_ontology(source, onto)     -> onto   (loads .omn file)

Query helpers:
  instances_of(cls, direct=False)             -> list
  classes_matching(expr_str, onto)            -> list
  (World).manchester_query(expr_str)          -> list
"""

import re

# ─────────────────────────────────────────────────────────────────────────────
# Well-known prefix tables
# ─────────────────────────────────────────────────────────────────────────────

_BUILTIN_PREFIXES = [          # (namespace_iri, short_prefix)  — order matters
    ("http://www.w3.org/2002/07/owl#",              "owl:"),
    ("http://www.w3.org/1999/02/22-rdf-syntax-ns#", "rdf:"),
    ("http://www.w3.org/2000/01/rdf-schema#",       "rdfs:"),
    ("http://www.w3.org/2001/XMLSchema#",           "xsd:"),
]

_STD_NS = {                    # prefix -> namespace IRI (for parser)
    "owl":  "http://www.w3.org/2002/07/owl#",
    "rdf":  "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd":  "http://www.w3.org/2001/XMLSchema#",
}

# xsd local name -> Python type
_XSD_TO_PY = {
    "string": str, "normalizedString": str, "token": str,
    "anyURI": str, "language": str, "Name": str, "NCName": str,
    "dateTime": str, "date": str, "time": str, "gYearMonth": str,
    "gYear": str, "gMonthDay": str, "gDay": str, "gMonth": str,
    "base64Binary": str, "hexBinary": str,
    "boolean":          bool,
    "integer":          int, "int": int, "long": int, "short": int, "byte": int,
    "nonNegativeInteger": int, "positiveInteger": int,
    "nonPositiveInteger": int, "negativeInteger": int,
    "unsignedLong": int, "unsignedInt": int,
    "unsignedShort": int, "unsignedByte": int,
    "decimal": float, "float": float, "double": float,
}

_PY_TO_XSD = {str: "xsd:string", int: "xsd:integer",
              float: "xsd:double", bool: "xsd:boolean"}

# Mapping from owlready2 Python kwarg names → Manchester OWL 2 facet tokens
_FACET_TO_MANCHESTER = {
    "min_inclusive":   ">=",
    "max_inclusive":   "<=",
    "min_exclusive":   ">",
    "max_exclusive":   "<",
    "length":          "length",
    "min_length":      "minLength",
    "max_length":      "maxLength",
    "pattern":         "pattern",
    "total_digits":    "totalDigits",
    "fraction_digits": "fractionDigits",
    "white_space":     "whiteSpace",
}
# Reverse: Manchester facet token → Python kwarg name
_MANCHESTER_TO_FACET = {v: k for k, v in _FACET_TO_MANCHESTER.items()}

_CHARACTERISTICS = {
    "Functional":        "FunctionalProperty",
    "InverseFunctional": "InverseFunctionalProperty",
    "Transitive":        "TransitiveProperty",
    "Symmetric":         "SymmetricProperty",
    "Asymmetric":        "AsymmetricProperty",
    "Reflexive":         "ReflexiveProperty",
    "Irreflexive":       "IrreflexiveProperty",
}

# ─────────────────────────────────────────────────────────────────────────────
# 2a: Serialiser
# ─────────────────────────────────────────────────────────────────────────────

def _iri_to_short(iri, extra_prefixes=None):
    """Shorten an IRI using well-known or user-supplied prefixes."""
    if extra_prefixes:
        for ns, pfx in extra_prefixes:
            if iri.startswith(ns):
                return pfx + iri[len(ns):]
    for ns, pfx in _BUILTIN_PREFIXES:
        if iri.startswith(ns):
            return pfx + iri[len(ns):]
    # Auto-derive a prefix from the IRI namespace segment
    for sep in ("#", "/"):
        if sep in iri:
            ns_part, local = iri.rsplit(sep, 1)
            if local:
                if "/" in ns_part:
                    pfx_candidate = ns_part.rsplit("/", 1)[-1]
                    if pfx_candidate and re.match(r'^[A-Za-z]\w*$', pfx_candidate):
                        return "%s:%s" % (pfx_candidate, local)
                return local
    return "<%s>" % iri


def _literal_to_manchester(value):
    """Serialise a Python literal to Manchester OWL syntax."""
    from owlready2.util import locstr as _locstr
    if isinstance(value, _locstr):
        escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
        if value.lang:
            return '"%s"@%s' % (escaped, value.lang)
        return '"%s"' % escaped
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return '"%s"' % escaped
    return repr(value)


def _entity_label(entity, prefixes=None):
    """Return short Manchester name for any owlready2 entity or Python type."""
    if entity is None:
        return "owl:Nothing"
    if entity in _PY_TO_XSD:
        return _PY_TO_XSD[entity]
    # Inverse construct: serialize as  inverse (prop)
    try:
        from owlready2.class_construct import Inverse as _Inv
        if isinstance(entity, _Inv):
            return "inverse (%s)" % _iri_to_short(entity.property.iri, prefixes)
    except Exception:
        pass
    iri = getattr(entity, "iri", None)
    if iri is None:
        # Fall back to literal serialisation for Python scalars
        return _literal_to_manchester(entity)
    return _iri_to_short(iri, prefixes)


# Operator precedence for parenthesisation decisions
_PREC_OR   = 1
_PREC_AND  = 2
_PREC_NOT  = 3
_PREC_ATOM = 4


def _prec(expr):
    from owlready2.class_construct import And, Or, Not
    if isinstance(expr, Or):  return _PREC_OR
    if isinstance(expr, And): return _PREC_AND
    if isinstance(expr, Not): return _PREC_NOT
    return _PREC_ATOM


def to_manchester(expr, prefixes=None):
    """Serialise an owlready2 class expression to a Manchester OWL syntax string.

    Parameters
    ----------
    expr     : class / individual / class-construct / Python type
    prefixes : optional list of (namespace_iri, prefix_str) pairs used for
               shortening IRIs beyond the built-ins (owl:, rdf:, rdfs:, xsd:)

    Returns
    -------
    str
    """
    from owlready2.class_construct import (And, Or, Not, Restriction,
                                           OneOf, ConstrainedDatatype,
                                           Inverse, _PY_FACETS)
    from owlready2.base import SOME, ONLY, VALUE, HAS_SELF, EXACTLY, MIN, MAX

    if expr is None:
        return "owl:Nothing"
    if expr in _PY_TO_XSD:
        return _PY_TO_XSD[expr]

    # Named entity (OWL class, property, individual)
    if not isinstance(expr, (And, Or, Not, Restriction, OneOf,
                              ConstrainedDatatype, Inverse)):
        return _entity_label(expr, prefixes)

    if isinstance(expr, And):
        parts = []
        for c in expr.Classes:
            s = to_manchester(c, prefixes)
            # Parenthesize lower-precedence sub-expressions AND multi-word
            # restrictions for readability (matches owlapy / Protege output)
            if _prec(c) < _PREC_AND or isinstance(c, Restriction):
                s = "(%s)" % s
            parts.append(s)
        return " and ".join(parts)

    if isinstance(expr, Or):
        parts = []
        for c in expr.Classes:
            s = to_manchester(c, prefixes)
            if _prec(c) < _PREC_OR or isinstance(c, Restriction):
                s = "(%s)" % s
            parts.append(s)
        return " or ".join(parts)

    if isinstance(expr, Not):
        s = to_manchester(expr.Class, prefixes)
        if _prec(expr.Class) < _PREC_NOT:
            s = "(%s)" % s
        return "not " + s

    if isinstance(expr, Restriction):
        prop = _entity_label(expr.property, prefixes)
        v    = expr.value

        if expr.type == SOME:
            return "%s some %s" % (prop, _filler(v, prefixes))
        if expr.type == ONLY:
            return "%s only %s" % (prop, _filler(v, prefixes))
        if expr.type == VALUE:
            return "%s value %s" % (prop, _entity_label(v, prefixes))
        if expr.type == HAS_SELF:
            return "%s Self" % prop

        filler_str = ""
        if v is not None and getattr(v, "iri", None) != "http://www.w3.org/2002/07/owl#Thing":
            filler_str = " " + _filler(v, prefixes)
        n = expr.cardinality
        if expr.type == EXACTLY:
            return "%s exactly %d%s" % (prop, n, filler_str)
        if expr.type == MIN:
            return "%s min %d%s" % (prop, n, filler_str)
        if expr.type == MAX:
            return "%s max %d%s" % (prop, n, filler_str)

    if isinstance(expr, OneOf):
        return "{%s}" % " ".join(_entity_label(i, prefixes) for i in expr.instances)

    if isinstance(expr, ConstrainedDatatype):
        base  = _entity_label(expr.base_datatype, prefixes)
        facts = []
        for py_name, manc_token in _FACET_TO_MANCHESTER.items():
            val = getattr(expr, py_name, None)
            if val is not None:
                if manc_token == "pattern":
                    facts.append('pattern "%s"' % val)
                else:
                    facts.append("%s %s" % (manc_token, val))
        return ("%s[%s]" % (base, ", ".join(facts))) if facts else base



    if isinstance(expr, Inverse):
        return "inverse (%s)" % _entity_label(expr.property, prefixes)

    return repr(expr)


def _filler(v, prefixes):
    """Render restriction filler; wrap And/Or in parens (they are not primary)."""
    from owlready2.class_construct import And, Or
    s = to_manchester(v, prefixes)
    if isinstance(v, (And, Or)):
        s = "(%s)" % s
    return s


def manchester_render_ontology(onto, prefixes=None):
    """Render an entire ontology to Manchester OWL Notation (.omn) text.

    Parameters
    ----------
    onto     : owlready2.Ontology
    prefixes : optional list of (namespace_iri, prefix_str) pairs

    Returns
    -------
    str
    """
    import owlready2

    lines = []

    # Prefix declarations
    lines.append("Prefix: owl: <http://www.w3.org/2002/07/owl#>")
    lines.append("Prefix: rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>")
    lines.append("Prefix: rdfs: <http://www.w3.org/2000/01/rdf-schema#>")
    lines.append("Prefix: xsd: <http://www.w3.org/2001/XMLSchema#>")
    base_iri = onto.base_iri  # e.g. "http://example.org/animals#"
    onto_iri = base_iri.rstrip("#/")
    lines.append("Prefix: : <%s>" % base_iri)
    # Derive a named prefix from the last path segment (e.g. "animals:" for .../animals#)
    _stripped = onto_iri
    _derived_pfx = None
    if "/" in _stripped:
        _cand = _stripped.rsplit("/", 1)[-1]
        if _cand and re.match(r'^[A-Za-z]\w*$', _cand):
            _derived_pfx = _cand
    if _derived_pfx:
        lines.append("Prefix: %s: <%s>" % (_derived_pfx, base_iri))
    if prefixes:
        for ns, pfx in prefixes:
            lines.append("Prefix: %s <%s>" % (pfx.rstrip(":"), ns))
    lines.append("")

    # Ontology header
    lines.append("Ontology: <%s>" % onto_iri)
    for imp in onto.imported_ontologies:
        lines.append("    Import: <%s>" % imp.base_iri[:-1])
    lines.append("")

    # Object properties (before classes to avoid forward-reference issues)
    for prop in onto.object_properties():
        lines.append("ObjectProperty: %s" % _entity_label(prop, prefixes))
        if prop.domain:
            lines.append("    Domain: %s" % ", ".join(
                to_manchester(d, prefixes) for d in prop.domain))
        if prop.range:
            lines.append("    Range: %s" % ", ".join(
                _entity_label(r, prefixes) for r in prop.range))
        named_sup = [p for p in prop.is_a
                     if hasattr(p, "iri") and p is not owlready2.ObjectProperty]
        if named_sup:
            lines.append("    SubPropertyOf: %s" % ", ".join(
                _entity_label(p, prefixes) for p in named_sup))
        if prop.inverse_property:
            lines.append("    InverseOf: %s" % _entity_label(prop.inverse_property, prefixes))
        chars = _property_characteristics(prop)
        if chars:
            lines.append("    Characteristics: %s" % ", ".join(chars))
        lines.append("")

    # Data properties (before classes)
    for prop in onto.data_properties():
        lines.append("DataProperty: %s" % _entity_label(prop, prefixes))
        if prop.domain:
            lines.append("    Domain: %s" % ", ".join(
                to_manchester(d, prefixes) for d in prop.domain))
        if prop.range:
            lines.append("    Range: %s" % ", ".join(
                to_manchester(r, prefixes) for r in prop.range))
        chars = _property_characteristics(prop)
        if chars:
            lines.append("    Characteristics: %s" % ", ".join(chars))
        lines.append("")

    # Annotation properties (before classes, skip built-ins)
    for prop in onto.annotation_properties():
        if any(prop.iri.startswith(ns) for ns, _ in _BUILTIN_PREFIXES):
            continue
        lines.append("AnnotationProperty: %s" % _entity_label(prop, prefixes))
        lines.append("")

    # Classes
    for cls in onto.classes():
        lines.append("Class: %s" % _entity_label(cls, prefixes))
        # SubClassOf — restrictions / constructs (non-named superclasses)
        construct_parents = [p for p in cls.is_a
                             if not isinstance(p, owlready2.ThingClass)]
        if construct_parents:
            lines.append("    SubClassOf: %s" % ", ".join(
                to_manchester(p, prefixes) for p in construct_parents))
        # SubClassOf — named superclasses (excluding Thing)
        named_parents = [p for p in cls.is_a
                         if isinstance(p, owlready2.ThingClass)
                         and p is not owlready2.Thing]
        if named_parents:
            lines.append("    SubClassOf: %s" % ", ".join(
                _entity_label(p, prefixes) for p in named_parents))
        # EquivalentTo
        if cls.equivalent_to:
            lines.append("    EquivalentTo: %s" % ", ".join(
                to_manchester(e, prefixes) for e in cls.equivalent_to))
        # DisjointWith
        seen_disj = set()
        for d in cls.disjoints():
            for other in d.entities:
                if other is not cls and id(other) not in seen_disj:
                    seen_disj.add(id(other))
                    lines.append("    DisjointWith: %s" % _entity_label(other, prefixes))
        lines.append("")

    # Individuals
    for ind in onto.individuals():
        lines.append("Individual: %s" % _entity_label(ind, prefixes))
        types = [t for t in ind.is_a
                 if isinstance(t, owlready2.ThingClass) and t is not owlready2.Thing]
        if types:
            lines.append("    Types: %s" % ", ".join(
                _entity_label(t, prefixes) for t in types))
        lines.append("")

    return "\n".join(lines)


def _property_characteristics(prop):
    import owlready2
    _map = [
        (owlready2.FunctionalProperty,        "Functional"),
        (owlready2.InverseFunctionalProperty,  "InverseFunctional"),
        (owlready2.TransitiveProperty,         "Transitive"),
        (owlready2.SymmetricProperty,          "Symmetric"),
        (owlready2.AsymmetricProperty,         "Asymmetric"),
        (owlready2.ReflexiveProperty,          "Reflexive"),
        (owlready2.IrreflexiveProperty,        "Irreflexive"),
    ]
    return [label for cls, label in _map if issubclass(type(prop), cls)]


# ─────────────────────────────────────────────────────────────────────────────
# Tokeniser (shared by expression parser and file parser)
# ─────────────────────────────────────────────────────────────────────────────

_T_IRI    = "IRI"
_T_STRING = "STRING"
_T_NUMBER = "NUMBER"
_T_WORD   = "WORD"
_T_PUNCT  = "PUNCT"
_T_EOF    = "EOF"

_TOK_RE = re.compile(r"""
    (?P<COMMENT>  \#[^\n]*                                                          )
  | (?P<IRI>      <[^>]*>                                                           )
  | (?P<STRING>   "(?:[^"\\]|\\.)*"(?:\^\^(?:<[^>]*>|[A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?)|@[\w-]+)?)
  | (?P<FLOAT>    -?[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?                             )
  | (?P<NUMBER>   -?[0-9]+                                                          )
  | (?P<FACETOP>  >=|<=|>|<                                                         )
  | (?P<WORD>     [A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?                         )
  | (?P<COLON>    :                                                                  )
  | (?P<PUNCT>    [{}()\[\],\^@]                                                    )
  | (?P<WS>       \s+                                                               )
""", re.VERBOSE)


class _Token:
    __slots__ = ("type", "value", "line")

    def __init__(self, typ, val, line=0):
        self.type  = typ
        self.value = val
        self.line  = line

    def __repr__(self):
        return "Token(%s,%r)" % (self.type, self.value)


def _tokenize(text):
    """Yield _Token objects, skipping whitespace and comments."""
    line = 1
    for m in _TOK_RE.finditer(text):
        kind = m.lastgroup
        val  = m.group()
        line += val.count("\n")
        if kind in ("WS", "COMMENT"):
            continue
        if kind == "IRI":
            yield _Token(_T_IRI, val[1:-1], line)
        elif kind == "STRING":
            yield _Token(_T_STRING, val, line)
        elif kind == "FLOAT":
            yield _Token(_T_NUMBER, float(val), line)
        elif kind == "NUMBER":
            yield _Token(_T_NUMBER, int(val), line)
        elif kind in ("WORD", "COLON", "FACETOP"):
            yield _Token(_T_WORD, val, line)
        elif kind == "PUNCT":
            yield _Token(_T_PUNCT, val, line)
    yield _Token(_T_EOF, "", line)


# ─────────────────────────────────────────────────────────────────────────────
# 2b: Expression Parser
# ─────────────────────────────────────────────────────────────────────────────

class _ExprParser:
    """Recursive descent parser for a single Manchester class expression."""

    def __init__(self, tokens, ontology, pfx=None):
        self._toks  = list(tokens)
        self._pos   = 0
        self._onto  = ontology
        self._world = ontology.world
        self._pfx   = dict(_STD_NS)
        # Auto-discover prefixes from all ontologies in the world
        _builtin_iris = {ns for ns, _ in _BUILTIN_PREFIXES}
        for onto_obj in self._world.ontologies.values():
            base = getattr(onto_obj, "base_iri", None)
            if not base or base in _builtin_iris:
                continue
            stripped = base.rstrip("#/")
            if "/" in stripped:
                pfx_name = stripped.rsplit("/", 1)[-1]
                if pfx_name and re.match(r'^[A-Za-z]\w*$', pfx_name):
                    self._pfx.setdefault(pfx_name, base)
        if pfx:
            self._pfx.update(pfx)

    # ── token helpers ────────────────────────────────────────────────────────

    @property
    def _cur(self):
        return self._toks[self._pos] if self._pos < len(self._toks) \
               else _Token(_T_EOF, "")

    def _peek(self, n=1):
        idx = self._pos + n
        return self._toks[idx] if idx < len(self._toks) else _Token(_T_EOF, "")

    def _eat(self, val=None):
        tok = self._cur
        if val is not None and tok.value != val:
            raise SyntaxError("Expected %r, got %r (line %d)" %
                               (val, tok.value, tok.line))
        self._pos += 1
        return tok

    def _eat_type(self, typ):
        tok = self._cur
        if tok.type != typ:
            raise SyntaxError("Expected %s, got %r (line %d)" %
                               (typ, tok.value, tok.line))
        self._pos += 1
        return tok

    def _match(self, *vals):
        return self._cur.value in vals

    def _at_end(self):
        return self._cur.type == _T_EOF

    # ── IRI / entity resolution ───────────────────────────────────────────────

    def _full_iri(self, name):
        """Expand a prefixed name or bare name to a full IRI."""
        if ":" in name:
            pfx, local = name.split(":", 1)
            if pfx in self._pfx:
                return self._pfx[pfx] + local
            return name
        return self._pfx.get("", self._onto.base_iri) + name

    def _resolve(self, name):
        """Look up an owlready2 entity by name."""
        import owlready2
        if name in ("owl:Thing", "Thing"):    return owlready2.Thing
        if name in ("owl:Nothing", "Nothing"): return owlready2.Nothing
        if name.startswith("xsd:"):
            local = name[4:]
            if local in _XSD_TO_PY:
                return _XSD_TO_PY[local]
        iri = self._full_iri(name)
        return self._world[iri]

    # ── grammar ───────────────────────────────────────────────────────────────

    def parse(self):
        expr = self._description()
        if not self._at_end():
            raise SyntaxError("Unexpected token %r (line %d)" %
                               (self._cur.value, self._cur.line))
        return expr

    def _description(self):
        """description ::= conjunction ('or' conjunction)*"""
        from owlready2.class_construct import Or
        left = self._conjunction()
        if not self._match("or"):
            return left
        clauses = [left]
        while self._match("or"):
            self._eat()
            clauses.append(self._conjunction())
        return Or(clauses)

    def _conjunction(self):
        """conjunction ::= primary ('and' primary)*"""
        from owlready2.class_construct import And
        left = self._primary()
        if not self._match("and"):
            return left
        clauses = [left]
        while self._match("and"):
            self._eat()
            clauses.append(self._primary())
        return And(clauses)

    def _primary(self):
        """primary ::= 'not' primary | '(' description ')' | '{' … '}' | restriction_or_atomic"""
        from owlready2.class_construct import Not
        tok = self._cur

        if tok.value == "not":
            self._eat()
            return Not(self._primary())

        if tok.value == "(":
            self._eat("(")
            expr = self._description()
            self._eat(")")
            return expr

        if tok.value == "{":
            return self._oneof()

        if tok.type in (_T_WORD, _T_IRI):
            return self._restriction_or_atomic()

        raise SyntaxError("Unexpected token %r (line %d)" % (tok.value, tok.line))

    def _restriction_or_atomic(self):
        """Either a restriction  `Prop keyword [n] Filler` or a plain class."""
        from owlready2.base import SOME, ONLY, VALUE, HAS_SELF, EXACTLY, MIN, MAX
        from owlready2.class_construct import Restriction

        if self._cur.value == "inverse":
            self._eat()
            self._eat("(")
            prop = self._entity_tok()
            self._eat(")")
            from owlready2.class_construct import Inverse
            subject = Inverse(prop)
        else:
            subject = self._entity_tok()

        kw = self._cur.value
        if kw == "some":
            self._eat(); return Restriction(subject, SOME,    value=self._primary())
        if kw == "only":
            self._eat(); return Restriction(subject, ONLY,    value=self._primary())
        if kw == "value":
            self._eat(); return Restriction(subject, VALUE,   value=self._value_or_entity_tok())
        if kw == "Self":
            self._eat(); return Restriction(subject, HAS_SELF, value=True)
        if kw in ("min", "max", "exactly"):
            self._eat()
            n     = self._eat_type(_T_NUMBER).value
            _type = {"min": MIN, "max": MAX, "exactly": EXACTLY}[kw]
            filler = None
            # Optional filler: present unless next is a stop token
            if self._cur.type in (_T_WORD, _T_IRI) or self._cur.value in ("(", "{", "not"):
                if self._cur.value not in ("and", "or", "some", "only", "value",
                                           "Self", "min", "max", "exactly",
                                           ",", ")", "]", ""):
                    filler = self._primary()
            return Restriction(subject, _type, cardinality=n, value=filler)

        # No restriction keyword — subject is itself the class expression
        return subject

    def _entity_tok(self):
        """Consume one name or IRI token and resolve it (+ optional CDT facets)."""
        tok = self._cur
        if tok.type == _T_IRI:
            self._eat()
            return self._world[tok.value]
        if tok.type == _T_WORD:
            self._eat()
            resolved = self._resolve(tok.value)
            # Check for constrained datatype: xsd:SomeType[facet val, ...]
            if resolved in (int, float, str, bool) and self._cur.value == "[":
                return self._constrained_datatype(resolved)
            return resolved
        raise SyntaxError("Expected name/IRI, got %r (line %d)" %
                          (tok.value, tok.line))

    def _value_or_entity_tok(self):
        """Consume a VALUE restriction filler: entity, literal, or typed/lang string."""
        tok = self._cur
        if tok.type == _T_NUMBER:
            self._eat()
            return tok.value
        if tok.type == _T_STRING:
            self._eat()
            return self._parse_literal(tok.value)
        if tok.type in (_T_WORD, _T_IRI):
            return self._entity_tok()
        raise SyntaxError("Expected value or entity, got %r (line %d)" %
                          (tok.value, tok.line))

    def _parse_literal(self, s):
        """Parse a Manchester string token into a typed Python value."""
        from owlready2.util import locstr as _locstr
        # Strip outer quotes
        end_q = s.rfind('"', 1)
        content = s[1:end_q].replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
        suffix  = s[end_q + 1:]
        if suffix.startswith("@"):
            return _locstr(content, lang=suffix[1:])
        if suffix.startswith("^^"):
            dtype_part = suffix[2:]
            if dtype_part.startswith("<") and dtype_part.endswith(">"):
                dtype_iri = dtype_part[1:-1]
            else:
                dtype_iri = self._full_iri(dtype_part)
            xsd = "http://www.w3.org/2001/XMLSchema#"
            if dtype_iri.startswith(xsd):
                local = dtype_iri[len(xsd):]
                if local in _XSD_TO_PY:
                    py_t = _XSD_TO_PY[local]
                    if py_t == int:   return int(content)
                    if py_t == float: return float(content)
                    if py_t == bool:  return content.lower() in ("true", "1")
            return content
        return content

    def _constrained_datatype(self, base_type):
        """Parse  [ facet val, ... ]  after a XSD base type."""
        from owlready2 import ConstrainedDatatype
        self._eat("[")
        facets = {}
        while self._cur.value != "]" and not self._at_end():
            # Facet token (>=, <=, >, <, length, minLength, etc.)
            facet_tok = self._cur.value
            if facet_tok not in _MANCHESTER_TO_FACET:
                raise SyntaxError("Unknown datatype facet %r (line %d)" %
                                  (facet_tok, self._cur.line))
            py_name = _MANCHESTER_TO_FACET[facet_tok]
            self._pos += 1
            # Facet value
            vt = self._cur
            if vt.type == _T_NUMBER:
                raw = vt.value
                self._pos += 1
            elif vt.type == _T_STRING:
                raw = _parse_string_literal(vt.value)
                self._pos += 1
            else:
                raise SyntaxError("Expected facet value, got %r (line %d)" %
                                  (vt.value, vt.line))
            # Coerce to correct Python type
            if py_name in ("min_inclusive", "max_inclusive",
                           "min_exclusive", "max_exclusive"):
                if base_type == float and isinstance(raw, int):
                    raw = float(raw)
                elif base_type == int and isinstance(raw, float):
                    raw = int(raw)
            elif py_name in ("length", "min_length", "max_length",
                             "total_digits", "fraction_digits"):
                raw = int(raw)
            facets[py_name] = raw
            if self._cur.value == ",":
                self._eat()
        self._eat("]")
        return ConstrainedDatatype(base_type, **facets)

    def _oneof(self):
        """{individual, individual, ...}"""
        from owlready2.class_construct import OneOf
        self._eat("{")
        inds = []
        while self._cur.value != "}":
            inds.append(self._entity_tok())
            if self._cur.value == ",":
                self._eat()
        self._eat("}")
        return OneOf(inds)


def parse_manchester_expression(text, ontology, prefixes=None):
    """Parse a Manchester OWL class expression into an owlready2 construct.

    Parameters
    ----------
    text      : str  — e.g. ``"hasPart some (Cat and Pet)"``
    ontology  : owlready2.Ontology
    prefixes  : dict  prefix_name -> namespace_iri  (optional)

    Returns
    -------
    owlready2 class expression
    """
    toks   = list(_tokenize(text))
    parser = _ExprParser(toks, ontology, prefixes)
    return parser.parse()


# ─────────────────────────────────────────────────────────────────────────────
# 2c: Manchester Ontology File (.omn) Parser
# ─────────────────────────────────────────────────────────────────────────────

def _local_name(iri):
    for sep in ("#", "/"):
        if sep in iri:
            return iri.rsplit(sep, 1)[-1]
    return iri


_FRAME_KW = frozenset([
    "Ontology", "Prefix", "Import",
    "Class", "ObjectProperty", "DataProperty", "AnnotationProperty",
    "Individual", "Datatype",
    "DisjointClasses", "EquivalentClasses",
    "DisjointProperties", "EquivalentProperties",
    "SameIndividual", "DifferentIndividuals",
])

_SECTION_KW = frozenset([
    "SubClassOf", "EquivalentTo", "DisjointWith", "DisjointUnionOf",
    "Domain", "Range", "SubPropertyOf", "SuperPropertyOf",
    "InverseOf", "Characteristics", "HasKey", "SubPropertyChain",
    "Types", "Facts", "SameAs", "DifferentFrom", "Annotations",
])


class _OmnParser:
    """Frame-oriented parser for OWL Manchester Notation (.omn) documents."""

    def __init__(self, text, ontology):
        self._toks  = list(_tokenize(text))
        self._pos   = 0
        self._onto  = ontology
        self._world = ontology.world
        self._pfx   = dict(_STD_NS)
        self._pfx[""] = ontology.base_iri

    # ── token helpers ────────────────────────────────────────────────────────

    @property
    def _cur(self):
        return self._toks[self._pos] if self._pos < len(self._toks) \
               else _Token(_T_EOF, "")

    def _peek(self, n=1):
        idx = self._pos + n
        return self._toks[idx] if idx < len(self._toks) else _Token(_T_EOF, "")

    def _eat(self, val=None):
        tok = self._cur
        if val is not None and tok.value != val:
            raise SyntaxError("Expected %r, got %r (line %d)" %
                               (val, tok.value, tok.line))
        self._pos += 1
        return tok

    def _at_end(self):
        return self._cur.type == _T_EOF

    def _is_frame(self):
        return (self._cur.type == _T_WORD
                and self._cur.value in _FRAME_KW
                and self._peek().value == ":")

    def _is_section(self):
        return (self._cur.type == _T_WORD
                and self._cur.value in _SECTION_KW
                and self._peek().value == ":")

    # ── IRI resolution ────────────────────────────────────────────────────────

    def _iri(self, name):
        if name.startswith("<") and name.endswith(">"):
            return name[1:-1]
        if ":" in name:
            pfx, local = name.split(":", 1)
            if pfx in self._pfx:
                return self._pfx[pfx] + local
        return self._pfx.get("", self._onto.base_iri) + name

    def _eat_iri(self):
        tok = self._cur
        self._pos += 1
        if tok.type == _T_IRI:
            return tok.value
        return self._iri(tok.value)

    def _get_or_create(self, iri, base_cls):
        import owlready2
        entity = self._world[iri]
        if entity is not None:
            return entity
        with self._onto:
            return type(_local_name(iri), (base_cls,), {})

    # ── expression slicer (delegates to _ExprParser) ─────────────────────────

    def _parse_expr(self):
        """Consume tokens for one expression, stopping at , / section / frame / EOF."""
        start = self._pos
        depth = 0
        while not self._at_end():
            v = self._cur.value
            if v in ("(", "{", "["):    depth += 1
            elif v in (")", "}", "]"):
                if depth == 0:          break
                depth -= 1
            elif depth == 0:
                if v == ",":            break
                if self._is_frame() or self._is_section():
                    break
            self._pos += 1

        subtoks = self._toks[start:self._pos]
        if not subtoks:
            return None
        # Strip trailing noise
        while subtoks and subtoks[-1].type in (_T_EOF,):
            subtoks.pop()
        if not subtoks:
            return None
        parser = _ExprParser(subtoks + [_Token(_T_EOF, "")], self._onto, self._pfx)
        try:
            return parser.parse()
        except SyntaxError:
            return None

    def _parse_expr_list(self):
        exprs = []
        e = self._parse_expr()
        if e is not None:
            exprs.append(e)
        while self._cur.value == ",":
            self._eat()
            e = self._parse_expr()
            if e is not None:
                exprs.append(e)
        return exprs

    def _skip_section(self):
        """Skip tokens until the next section/frame keyword or EOF."""
        depth = 0
        while not self._at_end():
            v = self._cur.value
            if v in ("(", "{", "["):    depth += 1
            elif v in (")", "}", "]"):  depth -= 1
            elif depth == 0 and (self._is_frame() or self._is_section()):
                break
            self._pos += 1

    # ── pre-pass: create entity stubs before axioms are processed ────────────

    def _pre_create_entities(self):
        """Linear scan: collect Prefix declarations, then pre-create all entity
        stubs so forward references in class expressions resolve correctly."""
        import owlready2
        _entity_kw = {
            "Class":              owlready2.Thing,
            "ObjectProperty":     owlready2.ObjectProperty,
            "DataProperty":       owlready2.DataProperty,
            "AnnotationProperty": owlready2.AnnotationProperty,
            "Individual":         owlready2.Thing,
        }
        toks = self._toks
        n    = len(toks)

        # ── sub-pass 1: collect Prefix declarations ──────────────────────────
        i = 0
        while i < n:
            tok = toks[i]
            if (tok.type == _T_WORD and tok.value == "Prefix"
                    and i + 1 < n and toks[i + 1].value == ":"):
                i += 2  # skip "Prefix" and ":"
                if i >= n:
                    break
                name_tok = toks[i]
                name = name_tok.value
                i += 1
                if name.endswith(":"):
                    name = name[:-1]
                elif i < n and toks[i].value == ":":
                    i += 1
                if i < n and toks[i].type == _T_IRI:
                    self._pfx[name] = toks[i].value
                    i += 1
            else:
                i += 1

        # ── sub-pass 2: create entity stubs ──────────────────────────────────
        i = 0
        while i < n:
            tok = toks[i]
            if (tok.type == _T_WORD and tok.value in _entity_kw
                    and i + 1 < n and toks[i + 1].value == ":"):
                base_cls = _entity_kw[tok.value]
                i += 2  # skip keyword and ":"
                if i >= n:
                    break
                iri_tok = toks[i]
                i += 1
                iri = iri_tok.value if iri_tok.type == _T_IRI else self._iri(iri_tok.value)
                self._get_or_create(iri, base_cls)
            else:
                i += 1

    # ── top-level dispatch ───────────────────────────────────────────────────

    def parse(self):
        self._pre_create_entities()
        self._pos = 0
        while not self._at_end():
            if not self._is_frame():
                self._pos += 1
                continue
            kw = self._eat().value
            self._eat(":")
            if   kw == "Prefix":                  self._prefix()
            elif kw == "Ontology":                self._ontology_header()
            elif kw == "Import":                  self._import()
            elif kw == "Class":                   self._class_frame()
            elif kw == "ObjectProperty":          self._property_frame("object")
            elif kw == "DataProperty":            self._property_frame("data")
            elif kw == "AnnotationProperty":      self._property_frame("annotation")
            elif kw == "Individual":              self._individual_frame()
            elif kw in ("DisjointClasses", "EquivalentClasses",
                        "DisjointProperties", "EquivalentProperties",
                        "SameIndividual", "DifferentIndividuals"):
                self._misc_frame(kw)
            else:
                self._skip_section()

    # ── frame handlers ───────────────────────────────────────────────────────

    def _prefix(self):
        """Prefix: name: <iri>"""
        # name can be "owl", "rdf", etc.  or just ":"
        name_tok = self._cur
        self._pos += 1
        name = name_tok.value
        if name.endswith(":"):
            name = name[:-1]
        elif self._cur.value == ":":
            self._eat(":")
        iri = self._eat_iri()
        self._pfx[name] = iri

    def _ontology_header(self):
        if self._cur.type == _T_IRI:
            self._eat()
        if self._cur.type == _T_IRI:
            self._eat()   # optional version IRI
        while self._is_section() or (self._cur.type == _T_WORD
                                     and self._cur.value in ("Import", "Annotations")):
            kw = self._eat().value
            self._eat(":")
            if kw == "Import":
                iri = self._eat_iri()
                try:
                    imp = self._world.get_ontology(iri).load()
                    if imp not in self._onto.imported_ontologies:
                        self._onto.imported_ontologies.append(imp)
                except Exception:
                    pass
            else:
                self._skip_section()

    def _import(self):
        iri = self._eat_iri()
        try:
            imp = self._world.get_ontology(iri).load()
            if imp not in self._onto.imported_ontologies:
                self._onto.imported_ontologies.append(imp)
        except Exception:
            pass

    def _class_frame(self):
        import owlready2
        iri = self._eat_iri()
        cls = self._get_or_create(iri, owlready2.Thing)
        with self._onto:
            while self._is_section():
                sec = self._eat().value
                self._eat(":")
                if sec == "SubClassOf":
                    for e in self._parse_expr_list():
                        if e not in cls.is_a:
                            cls.is_a.append(e)
                elif sec == "EquivalentTo":
                    for e in self._parse_expr_list():
                        if e not in cls.equivalent_to:
                            cls.equivalent_to.append(e)
                elif sec == "DisjointWith":
                    for e in self._parse_expr_list():
                        owlready2.AllDisjoint([cls, e])
                elif sec == "DisjointUnionOf":
                    parts = self._parse_expr_list()
                    if parts:
                        cls.equivalent_to.append(owlready2.Or(parts))
                else:
                    self._skip_section()

    def _property_frame(self, kind):
        import owlready2
        iri  = self._eat_iri()
        base = {"object": owlready2.ObjectProperty,
                "data":   owlready2.DataProperty,
                "annotation": owlready2.AnnotationProperty}[kind]
        prop = self._get_or_create(iri, base)
        with self._onto:
            while self._is_section():
                sec = self._eat().value
                self._eat(":")
                if sec == "Domain":
                    for e in self._parse_expr_list():
                        if e not in prop.domain:
                            prop.domain.append(e)
                elif sec == "Range":
                    for e in self._parse_expr_list():
                        if e not in prop.range:
                            prop.range.append(e)
                elif sec == "SubPropertyOf":
                    for e in self._parse_expr_list():
                        if e not in prop.is_a:
                            prop.is_a.append(e)
                elif sec == "InverseOf":
                    exprs = self._parse_expr_list()
                    if exprs and hasattr(prop, "inverse_property"):
                        prop.inverse_property = exprs[0]
                elif sec == "Characteristics":
                    self._characteristics(prop)
                else:
                    self._skip_section()

    def _characteristics(self, prop):
        import owlready2
        _map = {
            "Functional":        owlready2.FunctionalProperty,
            "InverseFunctional": owlready2.InverseFunctionalProperty,
            "Transitive":        owlready2.TransitiveProperty,
            "Symmetric":         owlready2.SymmetricProperty,
            "Asymmetric":        owlready2.AsymmetricProperty,
            "Reflexive":         owlready2.ReflexiveProperty,
            "Irreflexive":       owlready2.IrreflexiveProperty,
        }
        while (not self._at_end() and self._cur.type == _T_WORD
               and not self._is_section() and not self._is_frame()):
            name = self._eat().value
            mixin = _map.get(name)
            if mixin and not issubclass(type(prop), mixin):
                prop.is_a.append(mixin)
            if self._cur.value == ",":
                self._eat()

    def _individual_frame(self):
        import owlready2
        iri = self._eat_iri()
        entity = self._world.get(iri)
        if entity is None:
            entity = owlready2.Thing(_local_name(iri), namespace=self._onto)
        with self._onto:
            while self._is_section():
                sec = self._eat().value
                self._eat(":")
                if sec == "Types":
                    for e in self._parse_expr_list():
                        if e not in entity.is_a:
                            entity.is_a.append(e)
                elif sec == "Facts":
                    self._facts(entity)
                else:
                    self._skip_section()

    def _facts(self, ind):
        """Facts: propName value, ..."""
        while (not self._at_end() and not self._is_section()
               and not self._is_frame()):
            if self._cur.value == ",":
                self._eat()
                continue
            if self._cur.type not in (_T_WORD, _T_IRI):
                break
            prop_iri = self._iri(self._eat().value)
            prop_obj = self._world.get(prop_iri)
            # value
            vt = self._cur
            if vt.type == _T_STRING:
                val = _parse_string_literal(self._eat().value)
            elif vt.type == _T_NUMBER:
                val = self._eat().value
            elif vt.type in (_T_WORD, _T_IRI):
                val = self._world.get(self._iri(self._eat().value))
            else:
                break
            if prop_obj is None or val is None:
                continue
            try:
                pn = prop_obj.python_name
                if prop_obj.is_functional_for(type(ind)):
                    setattr(ind, pn, val)
                else:
                    getattr(ind, pn).append(val)
            except Exception:
                pass

    def _misc_frame(self, kw):
        import owlready2
        exprs = self._parse_expr_list()
        if not exprs:
            return
        with self._onto:
            if kw in ("DisjointClasses", "DisjointProperties"):
                owlready2.AllDisjoint(exprs)
            elif kw == "EquivalentClasses" and len(exprs) >= 2:
                exprs[0].equivalent_to.append(exprs[1])
            elif kw == "DifferentIndividuals":
                owlready2.AllDifferent(exprs)


def _parse_string_literal(s):
    inner = s
    if inner.startswith('"'):
        end = inner.rfind('"', 1)
        inner = inner[1:end]
    return inner.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")


def parse_manchester_ontology(source, ontology):
    """Load a Manchester OWL Notation (.omn) document into *ontology*.

    Parameters
    ----------
    source   : str | readable file-like
    ontology : owlready2.Ontology

    Returns
    -------
    ontology  (modified in-place)
    """
    import os
    if hasattr(source, "read"):
        text = source.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8")
    elif isinstance(source, (str, bytes, os.PathLike)) and os.path.exists(str(source)):
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = source  # treat as raw Manchester text
    _OmnParser(text, ontology).parse()
    ontology.loaded = True
    return ontology


# ─────────────────────────────────────────────────────────────────────────────
# 2d: Query helpers
# ─────────────────────────────────────────────────────────────────────────────

def _eval_expr(expr, ind):
    """Return True if individual *ind* satisfies class expression *expr*.

    Handles all owlready2 class constructs including Restriction fillers that
    are plain Python types (float, int, str) representing XSD datatypes —
    which the built-in _satisfied_by() does not support.
    """
    from owlready2.class_construct import (
        And, Or, Not, Restriction, OneOf, ConstrainedDatatype,
        SOME, ONLY, VALUE, HAS_SELF, MIN, MAX, EXACTLY,
    )

    # Named OWL class
    if isinstance(expr, type):
        # ThingClass has _satisfied_by; plain Python types do not.
        if hasattr(expr, '_satisfied_by'):
            return expr._satisfied_by(ind)
        return isinstance(ind, expr)   # plain Python type (shouldn't appear at top level)

    if isinstance(expr, And):
        return all(_eval_expr(c, ind) for c in expr.Classes)

    if isinstance(expr, Or):
        return any(_eval_expr(c, ind) for c in expr.Classes)

    if isinstance(expr, Not):
        return not _eval_expr(expr.Class, ind)

    if isinstance(expr, OneOf):
        return ind in expr.instances

    if isinstance(expr, Restriction):
        prop = expr.property
        val  = expr.value

        if expr.type == VALUE:
            return val in prop[ind]

        if expr.type == HAS_SELF:
            return ind in prop[ind]

        raw_vals = list(prop[ind])

        def _filler_matches(obj):
            if isinstance(val, ConstrainedDatatype):
                return _eval_constrained(val, obj)
            # Plain Python type used as XSD datatype filler (e.g. float, int, str)
            if isinstance(val, type) and not hasattr(val, '_satisfied_by'):
                return isinstance(obj, val)
            return _eval_expr(val, obj)

        if expr.type == SOME:
            return any(_filler_matches(o) for o in raw_vals)

        if expr.type == ONLY:
            return all(_filler_matches(o) for o in raw_vals)

        count = sum(1 for o in raw_vals if _filler_matches(o))
        if   expr.type == MIN:    return count >= expr.cardinality
        elif expr.type == MAX:    return count <= expr.cardinality
        elif expr.type == EXACTLY: return count == expr.cardinality

    # Fallback for unknown constructs
    if hasattr(expr, '_satisfied_by'):
        try:
            return expr._satisfied_by(ind)
        except (AttributeError, TypeError):
            return False

    return False


def _eval_constrained(dtype, val):
    """Return True if *val* satisfies all facets of a ConstrainedDatatype."""
    from owlready2.class_construct import _PY_FACETS
    if not isinstance(val, dtype.base_datatype):
        return False
    for facet in _PY_FACETS:
        fval = getattr(dtype, facet, None)
        if fval is None:
            continue
        if facet == 'min_inclusive' and not (val >= fval): return False
        if facet == 'max_inclusive' and not (val <= fval): return False
        if facet == 'min_exclusive' and not (val >  fval): return False
        if facet == 'max_exclusive' and not (val <  fval): return False
        if facet == 'min_length'    and not (len(val) >= fval): return False
        if facet == 'max_length'    and not (len(val) <= fval): return False
        if facet == 'length'        and not (len(val) == fval): return False
        if facet == 'pattern':
            import re as _re
            if not _re.fullmatch(fval, str(val)): return False
    return True


def instances_of(cls, direct=False, ontology=None):
    """Return all individuals that are instances of *cls*.

    *cls* may be a named OWL class or an anonymous class expression
    (And, Or, Restriction, …) produced by parse_manchester_expression().
    For anonymous expressions an *ontology* must be supplied so the world
    can be determined.

    Parameters
    ----------
    cls      : owlready2 OWL class or class expression
    direct   : if False (default) also include instances of subclasses
    ontology : required when *cls* is an anonymous expression

    Returns
    -------
    list
    """
    # Resolve the world from either the class or the supplied ontology.
    if hasattr(cls, 'namespace'):
        world = cls.namespace.world
    elif ontology is not None:
        world = ontology.world
    else:
        raise ValueError(
            "instances_of: cannot determine world from an anonymous expression "
            "without an explicit ontology= argument"
        )

    # Named class: fast path using world.search(type=…)
    # Use isinstance(cls, type) to distinguish ThingClass instances (real OWL
    # classes) from anonymous constructs (And, Or, Restriction…) which also
    # happen to have a subclasses() method.
    if isinstance(cls, type):
        result = []
        seen   = set()

        def _collect(c):
            for ind in world.search(type=c):
                k = id(ind)
                if k not in seen:
                    seen.add(k)
                    result.append(ind)
            if not direct:
                for sub in c.subclasses():
                    _collect(sub)

        _collect(cls)
        return result

    # Anonymous expression: use our evaluator which handles all construct types
    # including Restriction fillers that are plain Python types (xsd datatypes).
    return [ind for ind in world.individuals() if _eval_expr(cls, ind)]


def classes_matching(expr_str, ontology):
    """Find classes whose axioms contain the Manchester expression *expr_str*.

    Returns classes C where *expr* appears in ``C.is_a`` or
    ``C.equivalent_to`` (recursively inside And / Or / Restriction).

    Parameters
    ----------
    expr_str : str
    ontology : owlready2.Ontology

    Returns
    -------
    list of OWL classes
    """
    expr   = parse_manchester_expression(expr_str, ontology)
    result = []
    for cls in ontology.world.classes():
        axioms = list(cls.is_a) + list(cls.equivalent_to)
        if any(_expr_contains(ax, expr) for ax in axioms):
            result.append(cls)
    return result


def _cdt_equal(a, b):
    """Content-based equality for ConstrainedDatatype (which lacks __eq__)."""
    try:
        from owlready2 import ConstrainedDatatype as _CDT
    except ImportError:
        return a == b
    if not (isinstance(a, _CDT) and isinstance(b, _CDT)):
        return a == b
    if a.base_datatype != b.base_datatype:
        return False
    for attr in ("min_inclusive", "max_inclusive", "min_exclusive", "max_exclusive",
                 "length", "min_length", "max_length", "pattern",
                 "total_digits", "fraction_digits", "white_space"):
        if getattr(a, attr, None) != getattr(b, attr, None):
            return False
    return True


def _expr_equal(a, b):
    """Structural equality for OWL class expressions, CDT-aware."""
    from owlready2.class_construct import Restriction
    from owlready2 import ConstrainedDatatype as _CDT
    if isinstance(a, _CDT) or isinstance(b, _CDT):
        return _cdt_equal(a, b)
    if isinstance(a, Restriction) and isinstance(b, Restriction):
        if a.property != b.property or a.type != b.type:
            return False
        # Use getattr so that lazily-loaded (triple-store backed) restrictions
        # have their value fetched via __getattr__ rather than returning None
        # from __dict__ before first access.
        av = getattr(a, "value", None)
        bv = getattr(b, "value", None)
        ac = getattr(a, "cardinality", None)
        bc = getattr(b, "cardinality", None)
        if ac != bc:
            return False
        if av is None and bv is None:
            return True
        if av is None or bv is None:
            return False
        return _expr_equal(av, bv)
    return a == b


def _expr_contains(haystack, needle):
    if _expr_equal(haystack, needle):
        return True
    from owlready2.class_construct import And, Or, Not, Restriction
    if isinstance(haystack, (And, Or)):
        return any(_expr_contains(c, needle) for c in haystack.Classes)
    if isinstance(haystack, Not):
        return _expr_contains(haystack.Class, needle)
    if isinstance(haystack, Restriction):
        v = getattr(haystack, "value", None)
        return (haystack.property == needle
                or (v is not None and _expr_contains(v, needle)))
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Integration patches (applied at import time)
# ─────────────────────────────────────────────────────────────────────────────

def _patch_driver():
    import owlready2.driver as drv

    _orig_guess = drv._guess_format

    def _new_guess(f):
        try:
            if f.seekable():
                s = f.read(500); f.seek(0)
            else:
                s = f.peek(500)
            if isinstance(s, bytes):
                s = s.decode("utf-8", errors="replace")
        except Exception:
            return _orig_guess(f)
        stripped = s.lstrip()
        if re.match(r"(\s*#[^\n]*\n\s*)*(Prefix|Ontology|Class|ObjectProperty"
                    r"|DataProperty|Individual|AnnotationProperty)\s*:", stripped):
            return "manchester"
        return _orig_guess(f)

    drv._guess_format = _new_guess

    _orig_parse = drv.BaseSubGraph.parse

    def _new_parse(self, f, format=None, delete_existing_triples=True,
                   default_base=""):
        fmt = format or drv._guess_format(f)
        if fmt != "manchester":
            return _orig_parse(self, f, format=format,
                               delete_existing_triples=delete_existing_triples,
                               default_base=default_base)
        text = f.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        parse_manchester_ontology(text, self.onto)
        return self.onto.base_iri

    drv.BaseSubGraph.parse = _new_parse


def _patch_save():
    import owlready2.driver as drv

    _orig_save = drv._save

    def _new_save(f, format, graph, filter=None):
        if format != "manchester":
            return _orig_save(f, format, graph, filter)
        onto = getattr(graph, "onto", None)
        if onto is None and hasattr(graph, "world") and graph.world:
            ontos = list(graph.world.ontologies.values())
            onto  = ontos[0] if ontos else None
        if onto is None:
            raise ValueError("Cannot determine ontology for Manchester serialisation.")
        text = manchester_render_ontology(onto)
        if isinstance(text, str) and hasattr(f, "mode") and "b" in getattr(f, "mode", ""):
            f.write(text.encode("utf-8"))
        else:
            f.write(text)

    drv._save = _new_save


def _patch_world():
    from owlready2.namespace import World

    def manchester_query(self, expr_str, ontology=None):
        """Find individuals matching a Manchester class expression.

        Parameters
        ----------
        expr_str  : str — Manchester expression
        ontology  : owlready2.Ontology (optional; uses first loaded ontology)

        Returns
        -------
        list of individuals
        """
        if ontology is None:
            ontos = list(self.ontologies.values())
            if not ontos:
                raise ValueError("No ontologies in this world.")
            ontology = ontos[0]

        import owlready2
        expr = parse_manchester_expression(expr_str, ontology)
        result, seen = [], set()

        # Named class: collect instances of it and all subclasses
        if isinstance(expr, owlready2.ThingClass):
            for ind in instances_of(expr):
                k = id(ind)
                if k not in seen:
                    seen.add(k); result.append(ind)
            return result

        # Complex expression: check every individual's types
        for ind in self.individuals():
            for t in list(ind.is_a):
                if _expr_contains(t, expr):
                    k = id(ind)
                    if k not in seen:
                        seen.add(k); result.append(ind)
                    break
        return result

    if not hasattr(World, "manchester_query"):
        World.manchester_query = manchester_query


# Apply all patches
_patch_driver()
_patch_save()
_patch_world()
