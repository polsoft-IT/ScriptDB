# -*- coding: utf-8 -*-
"""Advanced documentation search: thirteen ways to find a document.

`tabs/docs_engine.py` owns the index and can search its *content*, filtered by
category, type or tag. That is one way to find a document, and it is not enough:
a reader who knows the author, the version, the date, a phrase from a heading,
or only roughly how a word was spelled has no way in.

This module is the other twelve ways in.

    SEARCH_KINDS    the thirteen kinds, as a table
    parse_query     the query language, which names a kind and its filters
    run             one entry point that dispatches a parsed query to a match
    fuzzy_*         approximate matching (typo tolerance)
    semantic_*      content-meaning matching with no model and no network

Two design decisions carry the module:

    * **Nothing here needs the network or a model.** "Semantic" is built from
      the document's own vocabulary -- a term co-occurrence profile compared
      with cosine similarity -- so it understands that a search for "backup"
      reaches a document about "archiving copies" without a training set and
      without leaving the machine. It is weaker than an embedding model and it
      is honest about being so; what it never is unavailable.
    * **Every kind returns the same shape.** A search yields
      ``[{doc_id, score, why, kind}]``, so the tab, the CLI and a test all read
      one structure and a new kind needs no new plumbing.
"""

import datetime as dt
import math
import re

# ---------------------------------------------------------------------------
# The thirteen ways to search
# ---------------------------------------------------------------------------
# (key, label key, fallback, what it matches, the query it expects)
SEARCH_KINDS = (
    ("content", "docs_search_content", "Content",
     "words anywhere in the body",
     "backup copy"),
    ("section", "docs_search_section", "Sections",
     "the text of one section, by its own name or its words",
     "section:recovery or section=Recovery"),
    ("heading", "docs_search_heading", "Headings",
     "headings only, so a mention in the prose is not a hit",
     "heading:install"),
    ("tag", "docs_search_tag", "Tags",
     "tag values on the document",
     "tag:policy"),
    ("type", "docs_search_type", "Document type",
     "the document's type",
     "type:guide"),
    ("date", "docs_search_date", "Date",
     "the created or modified date, as a range or an exact day",
     "date:2024-01-01..2024-12-31 or after:2024-06-01"),
    ("author", "docs_search_author", "Author",
     "the author facet in the document's metadata",
     "author:kowalski"),
    ("version", "docs_search_version", "Version",
     "the version facet, exactly or as a range",
     "version:2.1 or version>=2.0"),
    ("history", "docs_search_history", "History",
     "changes recorded against a document over time",
     "history:reindex or changed:2024-05"),
    ("archive", "docs_search_archive", "Archive",
     "archived (or explicitly not archived) documents",
     "archive:yes or archive:no"),
    ("metadata", "docs_search_metadata", "Metadata",
     "the raw metadata: hash, size, path, mtime and any meta key",
     "hash:ab12cd size>40000 path:docs/"),
    ("fuzzy", "docs_search_fuzzy", "Fuzzy",
     "words within an edit distance of the query, so typos still find",
     "~recieve or fuzzy:seperate"),
    ("semantic", "docs_search_semantic", "Semantic",
     "documents whose vocabulary is close in meaning, not in spelling",
     "~backup copies of the database"),
)

SEARCH_KIND_KEYS = tuple(key for key, _lk, _f, _d, _e in SEARCH_KINDS)
SEARCH_KIND_LABELS = {key: fallback for key, _lk, fallback, _d, _e
                      in SEARCH_KINDS}
SEARCH_KIND_MEANINGS = {key: meaning for key, _lk, _f, meaning, _e
                        in SEARCH_KINDS}
SEARCH_KIND_EXAMPLES = {key: example for key, _lk, _f, _d, example
                        in SEARCH_KINDS}

# The prefixes that name a kind, including the short spellings people type.
_KIND_ALIASES = {
    "content": "content", "text": "content", "body": "content",
    "section": "section", "sections": "section", "sec": "section",
    "heading": "heading", "headings": "heading", "head": "heading",
    "tag": "tag", "tags": "tag",
    "type": "type", "doctype": "type", "kind": "type",
    "date": "date", "after": "date", "before": "date", "between": "date",
    "author": "author", "by": "author", "owner": "author",
    "version": "version", "rev": "version", "ver": "version",
    "history": "history", "changed": "history", "log": "history",
    "archive": "archive", "archived": "archive",
    "metadata": "metadata", "meta": "metadata", "hash": "metadata",
    "size": "metadata", "path": "metadata", "mtime": "metadata",
    "fuzzy": "fuzzy", "like": "fuzzy", "approx": "fuzzy",
    "semantic": "semantic", "meaning": "semantic", "similar": "semantic",
}

# The comparison operators a numeric or dated field accepts.
_RANGE_OPERATORS = (">=", "<=", "!=", ">", "<", "=", ":")

# How many candidates a fuzzy or semantic search considers before ranking. A
# tolerance search has to look at more of the vocabulary than an exact one, and
# the cap is what keeps it from walking the whole index on every keystroke.
FUZZY_CANDIDATES = 400
SEMANTIC_CANDIDATES = 200

# The edit distance a fuzzy term still counts as a match. Scaled by the term's
# own length: two edits in a four-letter word would match almost anything, while
# two edits in a twelve-letter word is a plausible typo.
FUZZY_MIN_LENGTH = 3

# How much of a term's meaning a semantic hit must share. Deliberately low: the
# profile is crude, and a threshold that admits nothing is worse than one that
# admits a weak but explainable match (the score is shown, so the reader judges).
SEMANTIC_FLOOR = 0.12


# ---------------------------------------------------------------------------
# The query language
# ---------------------------------------------------------------------------
def parse_query(text, default_kind="content"):
    """A query as ``{kind, terms, filters, ranges, raw}``.

    The language is deliberately small and forgiving, because a search box is
    typed into rather than written in::

        backup copy                     content words
        heading:install                 one kind, one term
        section=Recovery                an exact section name
        tag:policy type:guide           several filters, all required
        date:2024-01-01..2024-12-31     a range with dots
        author:kowalski version>=2.0    operator forms
        hash:ab12cd size>40000          metadata, by any key
        ~recieve                        fuzzy (the tilde is the whole syntax)
        ~backup copies of the database  semantic

    A token that names no kind is a plain term, so `recovery procedure` keeps
    working exactly as it did before any of this existed.
    """
    raw = str(text or "").strip()
    parsed = {
        "kind": default_kind if default_kind in SEARCH_KIND_KEYS else "content",
        "terms": [],
        "filters": [],       # (kind, operator, value)
        "ranges": {},        # kind -> (low, high)
        "raw": raw,
    }
    if not raw:
        return parsed

    # The tilde asks for a tolerant search. One word under it means "I may
    # have misspelled this", which is fuzzy; several words under it mean "find
    # things close in meaning to this", which is semantic. The distinction is
    # made by counting the words rather than by two different sigils, because a
    # reader should not have to remember which one they want.
    if raw.startswith("~"):
        remainder = raw[1:].strip()
        parsed["kind"] = ("semantic" if len(remainder.split()) > 1
                          else "fuzzy")
        raw = remainder
    elif "~" in raw:
        parsed["kind"] = "fuzzy"
        raw = raw.replace("~", "")

    for token in _split_tokens(raw):
        kind, operator, value = _read_token(token)
        if kind is None:
            parsed["terms"].append(token.lower())
            continue
        if operator == "kind":
            # `fuzzy:word` / `semantic:words`: the text is the subject of the
            # kind, so it joins the terms and the kind is selected.
            parsed["terms"].append(value.lower())
            parsed["kind"] = kind
            continue
        if operator in (">=", "<=", ">", "<", "=", "after", "before"):
            parsed["filters"].append((kind, operator, value))
        elif ".." in value:
            low, _sep, high = value.partition("..")
            parsed["ranges"][kind] = (low.strip(), high.strip())
        else:
            # A bare `kind:value` is an equality. This branch must not swallow
            # `after:`/`before:`, which carry their own operator: coercing them
            # to `=` silently turns a range into a lookup that finds nothing.
            parsed["filters"].append((kind, "=", value))
        # A named kind wins over the default, so `heading:install` searches
        # headings even though the box defaults to content.
        if parsed["kind"] == "content" and kind != "content":
            parsed["kind"] = kind
    return parsed


def _split_tokens(text):
    """Query text split into tokens, keeping quoted phrases whole."""
    tokens = []
    for match in re.finditer(r'"([^"]*)"|(\S+)', str(text or "")):
        phrase, word = match.group(1), match.group(2)
        tokens.append(phrase.strip() if phrase is not None else word)
    return [token for token in tokens if token]


def _read_token(token):
    """One token as ``(kind, operator, value)``, ``(None, "", token)``."""
    for operator in ("..", ">=", "<=", ">", "<", "=", ":"):
        if operator not in token:
            continue
        head, _sep, tail = token.partition(operator)
        alias = head.strip().lower()
        kind = _KIND_ALIASES.get(alias)
        if kind is None or not tail.strip():
            continue
        # `fuzzy:word` and `semantic:words` name a search *kind* with the text
        # as its subject, not a field with a value. The text therefore has to
        # come back as a *term* for the kind's own matcher to read -- returning
        # it as a filter put it where that matcher never looks, and returning
        # the whole `fuzzy:word` as a term searched for a word nobody wrote.
        if kind in ("fuzzy", "semantic") and operator in (":", "="):
            return kind, "kind", tail.strip()
        # `after:` and `before:` name a kind *and* an ordering in one word, so
        # the alias is what carries the operator: mapping them to a plain `=`
        # would silently turn a date range into an exact-day lookup.
        if alias in ("after", "before"):
            return kind, alias, tail.strip()
        # `date:2024-01-01..2024-12-31` carries two operators: the colon that
        # names the kind and the dots that make the range.
        value = tail.strip()
        if operator in (":", "=") and ".." in value:
            return kind, "range", value
        return kind, operator, value
    return None, "", token


def kind_of(value):
    """The canonical kind for a name or alias (or None)."""
    return _KIND_ALIASES.get(str(value or "").strip().lower())


# ---------------------------------------------------------------------------
# Helpers over the indexed rows
# ---------------------------------------------------------------------------
def _row_get(row, key, default=""):
    """One value from a sqlite Row or a plain dict."""
    try:
        keys = row.keys()
    except AttributeError:
        keys = ()
    if key in keys:
        value = row[key]
        return default if value is None else value
    if isinstance(row, dict):
        value = row.get(key, default)
        return default if value is None else value
    return default


def _as_date(value):
    """A date from anything the docs use, as ``datetime.date`` or None.

    Accepts the forms that appear in practice: an ISO date, an ISO timestamp,
    and a bare year or year-month (which is expanded to the start of its month
    or year), so `date:2024` and `date:2024-05` both work without the user
    having to write a full date.
    """
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ").split(" ")[0].split(".")[0]
    for fmt, filler in (("%Y-%m-%d", None), ("%Y-%m", "-01"), ("%Y", "-01-01")):
        try:
            parsed = dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if filler is None:
            return parsed
        # Pad the partial form rather than fabricating a different day: a
        # `date:2024` search means the whole of 2024, and the *range* end is
        # computed by the caller, not by inventing December here.
        return dt.datetime.strptime(text + filler, "%Y-%m-%d").date()
    return None


def _as_number(value):
    """A value as a float, or None."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _matched_facets(engine, doc_id, kind, wanted):
    """The facet values of one kind on one document, as a set."""
    try:
        rows = engine.conn.execute(
            "SELECT value FROM docs_meta_index WHERE doc_id = ? AND facet = ?",
            (doc_id, kind)).fetchall()
    except Exception:
        return set()
    return {str(_row_get(row, "value")).lower() for row in rows}


def _facet_doc_ids(engine, kind, wanted, contains=False):
    """The document ids whose facet of `kind` matches `wanted`.

    `contains` is the forgiving mode: the facet value merely has to hold the
    wanted text, which is what an author or a tag search wants ("kow" finds
    "Kowalski") while a version search wants the exact spelling.
    """
    wanted = str(wanted or "").strip().lower()
    if not wanted:
        return None
    try:
        if contains:
            rows = engine.conn.execute(
                "SELECT DISTINCT doc_id FROM docs_meta_index "
                "WHERE facet = ? AND lower(value) LIKE ?",
                (kind, "%" + wanted + "%")).fetchall()
        else:
            rows = engine.conn.execute(
                "SELECT DISTINCT doc_id FROM docs_meta_index "
                "WHERE facet = ? AND lower(value) = ?",
                (kind, wanted)).fetchall()
    except Exception:
        return None
    return [row["doc_id"] for row in rows]


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------
def edit_distance(left, right, ceiling=None):
    """Levenshtein distance between two strings, capped at `ceiling`.

    Written by hand rather than pulled in, for the same reason the rest of the
    terminal is: one less thing that has to be present for a search to work.
    Two rows are kept rather than a full matrix, so a long query does not
    allocate one.
    """
    left = str(left or "")
    right = str(right or "")
    if left == right:
        return 0
    if not left or not right:
        return len(left) or len(right)
    if ceiling is not None and abs(len(left) - len(right)) > ceiling:
        return ceiling + 1
    previous = list(range(len(right) + 1))
    for i, character in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(
                previous[j] + 1,          # delete
                current[j - 1] + 1,       # insert
                previous[j - 1] + (character != other),  # substitute
            ))
        if ceiling is not None and min(current) > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


def fuzzy_allowance(term):
    """How many edits a term of this length is allowed.

    Scaled by length because a fixed allowance is wrong at both ends: one edit
    in a three-letter word matches half the vocabulary, while one edit in a
    twelve-letter word is a perfectly normal typo.

    A word at the minimum length gets no allowance either: at three characters
    a single edit reaches a large share of the language, so the search would
    return everything and mean nothing.
    """
    length = len(str(term or ""))
    if length <= FUZZY_MIN_LENGTH:
        return 0
    if length <= 4:
        return 1
    if length <= 8:
        return 2
    return 3


def fuzzy_terms(engine, term, limit=FUZZY_CANDIDATES):
    """The indexed terms within this term's edit allowance, best first.

    The vocabulary comes from the index the engine already keeps, so a fuzzy
    search costs no extra storage and cannot disagree with what is searchable.
    """
    wanted = str(term or "").strip().lower()
    allowance = fuzzy_allowance(wanted)
    if not wanted or allowance <= 0:
        return []
    vocabulary = _vocabulary(engine, limit)
    scored = []
    for candidate in vocabulary:
        distance = edit_distance(wanted, candidate, ceiling=allowance)
        if distance <= allowance:
            scored.append((distance, candidate))
            continue
        # A user types a stem, the document holds the inflected form: `instal`
        # against `installer` is three edits, which no edit allowance covers,
        # yet it is obviously what was meant. A prefix relation is therefore
        # checked as its own case rather than being left to the distance.
        if len(wanted) >= 4 and candidate.startswith(wanted[:max(4, len(wanted) - 1)]):
            scored.append((allowance + 1, candidate))
    scored.sort()
    return [term for _distance, term in scored[:limit]]


def _vocabulary(engine, limit=FUZZY_CANDIDATES):
    """The indexed vocabulary, most frequent first (or [] when unavailable)."""
    rows = []
    if not getattr(engine, "fts5", False):
        # The portable inverted index has a term table; read it first.
        try:
            rows = engine.conn.execute(
                "SELECT term FROM docs_terms ORDER BY freq DESC LIMIT ?",
                (limit * 4,)).fetchall()
        except Exception:
            rows = []
        if rows:
            return [str(_row_get(row, "term")) for row in rows]
    # An FTS5 build keeps no term table at all, so its words are read from the
    # cached plain text -- which is stored for exactly this kind of reuse. The
    # fallback runs whatever the engine reported, because the FTS5 case is the
    # common one and it must not silently lose its fuzzy search.
    try:
        rows = engine.conn.execute(
            "SELECT plain_text FROM docs_cache LIMIT ?",
            (max(limit, 200),)).fetchall()
    except Exception:
        return []
    words = {}
    for row in rows:
        for word in re.findall(r"[a-z0-9_]+", str(_row_get(row, "plain_text"))
                               .lower()):
            if len(word) >= FUZZY_MIN_LENGTH:
                words[word] = words.get(word, 0) + 1
    return [word for word, _count in sorted(words.items(),
                                            key=lambda item: -item[1])][:limit]


# ---------------------------------------------------------------------------
# Semantic matching
# ---------------------------------------------------------------------------
# The stop words a co-occurrence profile must ignore: they appear in every
# document, so they carry no meaning about which document is wanted.
_STOP_WORDS = frozenset("""
a an and are as at be by for from has have how i if in into is it its of on or
that the their then there these this to up was were what when where which who
will with you your not but can all any use used using one two more most other
""".split())


def semantic_profile(text, limit=None):
    """A term-frequency profile of a text, as ``{term: weight}``.

    The weight is a sublinear term frequency (``1 + log(count)``), which is the
    same idea as tf-idf without the corpus statistics: a word used ten times is
    more about the text than one used once, but not ten times more.
    """
    counts = {}
    for word in re.findall(r"[a-z0-9_]+", str(text or "").lower()):
        if len(word) < 3 or word in _STOP_WORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    profile = {word: 1.0 + math.log(count) for word, count in counts.items()}
    if limit and len(profile) > limit:
        top = sorted(profile.items(), key=lambda item: -item[1])[:limit]
        profile = dict(top)
    return profile


def cosine(left, right):
    """Cosine similarity of two profiles, in ``[0, 1]``.

    Zero when either side is empty, so a document that cannot be compared is
    never reported as a match rather than being given a meaningless score.
    """
    if not left or not right:
        return 0.0
    shared = set(left) & set(right)
    if not shared:
        return 0.0
    dot = sum(left[word] * right[word] for word in shared)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def semantic_related(engine, query, limit=SEMANTIC_CANDIDATES):
    """Terms that co-occur with the query across the corpus, for expansion.

    This is what lets a search for "backup" reach a document about "archiving
    copies": the words do not match, but they appear in the same documents, and
    the corpus says so. It is a co-occurrence proxy for meaning rather than a
    model of it, and the report says which terms were added so the answer is
    never a mystery.
    """
    query_terms = set(semantic_profile(query).keys())
    if not query_terms:
        return []
    try:
        rows = engine.conn.execute(
            "SELECT doc_id FROM docs_cache LIMIT ?", (limit,)).fetchall()
        ids = [row["doc_id"] for row in rows]
    except Exception:
        return []
    counts = {}
    for doc_id in ids:
        try:
            cached = engine.conn.execute(
                "SELECT plain_text FROM docs_cache WHERE doc_id = ?",
                (doc_id,)).fetchone()
        except Exception:
            continue
        if cached is None:
            continue
        words = set(semantic_profile(_row_get(cached, "plain_text")).keys())
        if not (words & query_terms):
            continue
        for word in words - query_terms:
            counts[word] = counts.get(word, 0) + 1
    # A term that appears with the query in many documents is a candidate
    # synonym for it; the ones that appear everywhere are not.
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    threshold = max(2, len(ids) // 4) if ids else 2
    return [word for word, count in ranked[:12] if count >= threshold]


# ---------------------------------------------------------------------------
# Running a search
# ---------------------------------------------------------------------------
def _result(doc_id, score, why, kind):
    """One search hit, in the shape every kind returns."""
    return {"doc_id": doc_id, "score": round(float(score), 4),
            "why": why, "kind": kind}


def _all_doc_ids(engine):
    """Every indexed document id (or [] when the index is empty)."""
    try:
        rows = engine.conn.execute("SELECT doc_id FROM docs_index").fetchall()
    except Exception:
        return []
    return [row["doc_id"] for row in rows]


def _rows_by_id(engine, ids):
    """``{doc_id: row}`` for the given ids, so a filter can read the fields."""
    if not ids:
        return {}
    try:
        marks = ",".join("?" for _ in ids)
        rows = engine.conn.execute(
            "SELECT * FROM docs_index WHERE doc_id IN (%s)" % marks,
            list(ids)).fetchall()
    except Exception:
        return {}
    return {row["doc_id"]: row for row in rows}


def run(engine, query, documents=None, limit=200):
    """Run a query (a string or the output of `parse_query`): the hits.

    `engine` is the `DocsIndex` from `tabs.docs_engine`. `documents` is needed
    only by the section, heading and history kinds, which read the live
    document bodies rather than the index (a heading is structure, and the
    index stores text, not structure). When it is absent those kinds report no
    hits rather than pretending the documents have no headings.
    """
    spec = query if isinstance(query, dict) else parse_query(query)
    kind = spec.get("kind", "content")
    runner = _RUNNERS.get(kind)
    if runner is None:
        return []
    hits = runner(engine, spec, documents, limit)
    # The filters are applied to every kind's result, so `author:x type:y`
    # narrows whatever the kind found rather than being ignored by the kinds
    # that do not know about authors.
    hits = _apply_filters(engine, hits, spec)
    hits.sort(key=lambda item: -item["score"])
    return hits[:limit]


def _apply_filters(engine, hits, spec):
    """Narrow a result set by the query's own filters, other than its kind."""
    filters = [item for item in spec.get("filters", [])
               if item[0] != spec.get("kind")]
    if not filters:
        return hits
    allowed = None
    for facet_kind, operator, value in filters:
        facet = {"author": "meta:author", "version": "meta:version",
                 "tag": "tag", "type": "type"}.get(facet_kind, facet_kind)
        ids = _facet_doc_ids(engine, facet, value, contains=(operator == ":"))
        if ids is None:
            continue
        allowed = set(ids) if allowed is None else (allowed & set(ids))
    if allowed is None:
        return hits
    return [hit for hit in hits if hit["doc_id"] in allowed]


def _search_content(engine, spec, documents, limit):
    terms = spec.get("terms") or []
    if not terms:
        return []
    docs = engine.search(" ".join(terms), limit=limit * 3)
    # The engine returns rows keyed `doc_id`; reading `id` there would silently
    # produce a hit with no document, which is worse than no hit at all.
    return [_result(doc.get("doc_id", doc.get("id")),
                    0.6 + 0.4 / (1 + index),
                    "content: " + " ".join(terms), "content")
            for index, doc in enumerate(docs)]


def _search_fuzzy(engine, spec, documents, limit):
    """Approximate matching: expand each word, then search on the expansions."""
    words = spec.get("terms") or []
    if not words:
        return []
    expanded = {}
    for word in words:
        found = fuzzy_terms(engine, word)
        if found:
            expanded[word] = found
    if not expanded:
        return []
    hits = {}
    for word, candidates in expanded.items():
        for candidate in candidates[:6]:
            docs = engine.search(candidate, limit=limit)
            distance = edit_distance(word, candidate)
            score = 1.0 - (distance / max(len(word), len(candidate), 1))
            for doc in docs:
                # The engine returns rows keyed `doc_id`; reading `id` yielded
                # a hit with no document at all, which is worse than no hit.
                doc_id = doc.get("doc_id", doc.get("id"))
                best = hits.get(doc_id)
                if best is None or score > best[0]:
                    hits[doc_id] = (score, "%s -> %s (edit %d)"
                                    % (word, candidate, distance))
    return [_result(doc_id, score, "fuzzy: " + why, "fuzzy")
            for doc_id, (score, why) in hits.items()]


def _search_semantic(engine, spec, documents, limit):
    """Meaning-based matching over the corpus's own co-occurrence profile."""
    words = spec.get("terms") or []
    query_text = " ".join(words)
    if not query_text:
        return []
    profile = semantic_profile(query_text)
    related = semantic_related(engine, query_text)
    # The profile is widened with the corpus's own neighbours, which is the
    # whole of the "understanding": a word that shares documents with the
    # query shares its meaning closely enough to search on.
    for word in related:
        profile.setdefault(word, 0.5)
    try:
        rows = engine.conn.execute(
            "SELECT doc_id, plain_text FROM docs_cache").fetchall()
    except Exception:
        return []
    hits = []
    for row in rows:
        doc_profile = semantic_profile(_row_get(row, "plain_text"))
        score = cosine(profile, doc_profile)
        if score >= SEMANTIC_FLOOR:
            shared = sorted(set(profile) & set(doc_profile))[:5]
            hits.append(_result(_row_get(row, "doc_id"), score,
                                "semantic: shared " + ", ".join(shared),
                                "semantic"))
    hits.sort(key=lambda item: -item["score"])
    return hits[:limit]


def _search_tag(engine, spec, documents, limit):
    wanted = _first_value(spec, "tag") or " ".join(spec.get("terms") or [])
    ids = _facet_doc_ids(engine, "tag", wanted, contains=True) or []
    return [_result(doc_id, 1.0, "tag: %s" % wanted, "tag") for doc_id in ids]


def _search_type(engine, spec, documents, limit):
    wanted = _first_value(spec, "type") or " ".join(spec.get("terms") or [])
    ids = _facet_doc_ids(engine, "type", wanted, contains=True) or []
    return [_result(doc_id, 1.0, "type: %s" % wanted, "type") for doc_id in ids]


def _search_author(engine, spec, documents, limit):
    wanted = _first_value(spec, "author") or " ".join(spec.get("terms") or [])
    ids = _facet_doc_ids(engine, "meta:author", wanted, contains=True) or []
    return [_result(doc_id, 1.0, "author: %s" % wanted, "author")
            for doc_id in ids]


def _search_version(engine, spec, documents, limit):
    """Version search, exact or as an ordered comparison."""
    operator, wanted = _first_filter(spec, "version")
    if wanted is None:
        wanted = " ".join(spec.get("terms") or [])
        operator = ":"
    if not wanted:
        return []
    if operator in (">", ">=", "<", "<="):
        return _version_compare(engine, operator, wanted)
    ids = _facet_doc_ids(engine, "meta:version", wanted, contains=True) or []
    return [_result(doc_id, 1.0, "version: %s" % wanted, "version")
            for doc_id in ids]


def _version_compare(engine, operator, wanted):
    """Documents whose version compares as asked.

    Versions are compared part by part, numerically where the parts are
    numbers, so 2.10 is above 2.9 rather than below it -- the mistake a plain
    string comparison makes and the one a reader would notice.
    """
    target = _version_parts(wanted)
    if not target:
        return []
    hits = []
    for doc_id in _all_doc_ids(engine):
        # Every version the document carries is compared, not the wanted one:
        # the point of the search is to find which documents *satisfy* the
        # comparison, which requires reading their own versions.
        for value in _version_values(engine, doc_id):
            parts = _version_parts(value)
            if not parts:
                continue
            order = _compare_parts(parts, target)
            if operator == ">":
                ok = order > 0
            elif operator == ">=":
                ok = order >= 0
            elif operator == "<":
                ok = order < 0
            else:
                ok = order <= 0
            if ok:
                hits.append(_result(doc_id, 1.0,
                                    "version %s is %s %s"
                                    % (value, operator, wanted), "version"))
                break
    return hits

def _version_values(engine, doc_id):
    """Every version a document records, from its meta facets."""
    try:
        rows = engine.conn.execute(
            "SELECT value FROM docs_meta_index "
            "WHERE doc_id = ? AND facet = ?", (doc_id, "meta:version"))
        return [str(_row_get(row, "value")) for row in rows.fetchall()]
    except Exception:
        return []


def _version_parts(value):
    """A version string as a list of ``(number, text)`` parts."""
    text = str(value or "").strip()
    if not text:
        return []
    parts = []
    for piece in re.split(r"[.\-_ ]+", text):
        number = _as_number(piece)
        parts.append((number, piece.lower()) if number is not None
                     else (None, piece.lower()))
    return parts


def _compare_parts(left, right):
    """``-1`` / ``0`` / ``1`` for two version part lists."""
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else (0, "")
        b = right[index] if index < len(right) else (0, "")
        if a[0] is not None and b[0] is not None:
            if a[0] != b[0]:
                return -1 if a[0] < b[0] else 1
        elif a[1] != b[1]:
            return -1 if a[1] < b[1] else 1
    return 0


def _search_date(engine, spec, documents, limit):
    """Date search: an exact day, a range, or after/before."""
    operator, wanted = _first_filter(spec, "date")
    low, high = spec.get("ranges", {}).get("date", (None, None))
    if wanted is None and low is None:
        wanted = " ".join(spec.get("terms") or [])
        operator = ":"
    if wanted is None and low is not None:
        operator, wanted = "range", low
    return _date_matches(engine, operator, wanted or "", low=low, high=high)


def _date_matches(engine, operator, wanted, low=None, high=None):
    """Documents whose created or modified date satisfies the request."""
    if operator in ("after", ">"):
        low, high = wanted, None
    elif operator in ("before", "<"):
        low, high = None, wanted
    elif operator == "range":
        pass
    elif wanted:
        low, high = wanted, wanted
    low_date = _as_date(low) if low else None
    high_date = _as_date(high) if high else None
    # A bare year or month means the whole of it, so the upper bound is the
    # last day the partial date can mean rather than the first.
    if high and high_date:
        high_date = _expand_end(high, high_date)
    hits = []
    for doc_id in _all_doc_ids(engine):
        for value in _date_fields(engine, doc_id):
            moment = _as_date(value)
            if moment is None:
                continue
            if low_date and moment < low_date:
                continue
            if high_date and moment > high_date:
                continue
            hits.append(_result(doc_id, 1.0, "date: %s" % value, "date"))
            break
    return hits


def _expand_end(text, parsed):
    """The last day a partial date can mean (a year, or a year and a month)."""
    text = str(text or "").strip()
    if re.fullmatch(r"\d{4}", text):
        return dt.date(parsed.year, 12, 31)
    if re.fullmatch(r"\d{4}-\d{2}", text):
        if parsed.month == 12:
            return dt.date(parsed.year, 12, 31)
        return dt.date(parsed.year, parsed.month + 1, 1) - dt.timedelta(days=1)
    return parsed


def _date_fields(engine, doc_id):
    """The dates a document carries, as facts about the document.

    Only the document's own metadata is read -- `created`, `modified`,
    `date` and `updated` -- and deliberately not the index's `updated`
    column. That column records when the *catalogue* last saw the document,
    which is "now" for every row after a reindex, so including it made
    `before:<any date>` match the whole repository. The distinction is
    between a fact about the document and a fact about the index.
    """
    values = []
    for facet in ("meta:created", "meta:modified", "meta:date",
                  "meta:updated"):
        values.extend(_matched_facets(engine, doc_id, facet, ""))
    return [value for value in values if value]


def _search_history(engine, spec, documents, limit):
    """History search: what changed against a document, and when.

    The history lives in the meta facets the index already keeps
    (`meta:history` and `meta:changed`), so a document whose metadata records
    its own changes is searchable by them without a second store.
    """
    wanted = (_first_value(spec, "history")
              or " ".join(spec.get("terms") or []))
    if not wanted:
        return []
    if re.fullmatch(r"\d{4}(-\d{2})?", wanted):
        # A month or year in a history search looks in the recorded history,
        # *not* in the document's `updated` column: every document has an
        # updated timestamp, so falling through to the date search would match
        # the whole repository and answer a different question than the one
        # asked.
        return _history_date_matches(engine, wanted)
    hits = []
    for doc_id in _all_doc_ids(engine):
        for facet in ("meta:history", "meta:changed", "meta:revision"):
            for value in _matched_facets(engine, doc_id, facet, ""):
                if wanted.lower() in value:
                    hits.append(_result(doc_id, 1.0,
                                        "history: %s" % value, "history"))
                    break
    return hits


def _history_date_matches(engine, wanted):
    """Documents whose recorded history names the month or year asked for."""
    hits = []
    for doc_id in _all_doc_ids(engine):
        for facet in ("meta:history", "meta:changed", "meta:revision"):
            for value in _matched_facets(engine, doc_id, facet, ""):
                if wanted in value:
                    hits.append(_result(doc_id, 1.0,
                                        "history: %s" % value, "history"))
                    break
    return hits

def _search_archive(engine, spec, documents, limit):
    """Archived (or explicitly live) documents."""
    wanted = (_first_value(spec, "archive")
              or " ".join(spec.get("terms") or "")).lower()
    if wanted in ("no", "false", "0", "live", "current", "active"):
        return _archive_matches(engine, wanted=False)
    if wanted in ("yes", "true", "1", "archived", "archive", ""):
        return _archive_matches(engine, wanted=True)
    if not wanted:
        # A bare `archive:` with nothing after it is the archived set: that is
        # the unusual state, and therefore the one worth searching for.
        return _archive_matches(engine, wanted=True)
    # An explicit "all" is the one way to ask for either: the default of a bare
    # `archive:` is archived documents, because that is the unusual state and
    # therefore the one worth searching for.
    if wanted in ("all", "any", "*"):
        return (_archive_matches(engine, True) + _archive_matches(engine, False))
    return []


def _archive_matches(engine, wanted):
    """Archived documents, or the ones that are *not* archived.

    A document is archived when it carries an `archived` facet that is truthy.
    "Not archived" therefore cannot be found by looking for a `false` value: a
    live document has no such facet at all, which is the whole difficulty and
    the reason the set is computed by subtraction rather than by a lookup.
    """
    archived = []
    for doc_id in _all_doc_ids(engine):
        values = {value.lower() for value in
                  _matched_facets(engine, doc_id, "meta:archived", "")}
        if values & {"true", "1", "yes", "on"}:
            archived.append(doc_id)
    ids = archived if wanted else [doc_id for doc_id in _all_doc_ids(engine)
                                   if doc_id not in set(archived)]
    return [_result(doc_id, 1.0,
                    "archive: %s" % ("yes" if wanted else "no"), "archive")
            for doc_id in ids]


def _search_metadata(engine, spec, documents, limit):
    """Metadata search: hash, size, path, mtime and any meta key.

    The indexed columns are read directly for the ones the index owns (a hash
    is a column, not a facet) and the meta facets for the rest, so a search
    never has to know which is which in advance.
    """
    filters = list(spec.get("filters", [])) + list(spec.get("ranges", {}).items())
    wanted_terms = " ".join(spec.get("terms") or [])
    hits = {}
    for entry in filters:
        kind, operator, value = _read_metadata_filter(entry)
        if not kind:
            continue
        for doc_id, why in _metadata_matches(engine, kind, operator, value):
            hits[doc_id] = _result(doc_id, 1.0, "metadata: " + why, "metadata")
    if not hits and wanted_terms:
        # A bare term in a metadata search looks in the metadata text itself,
        # which is what a reader means by "search the metadata".
        for doc_id, why in _metadata_matches(engine, "any", ":", wanted_terms):
            hits[doc_id] = _result(doc_id, 1.0, "metadata: " + why, "metadata")
    return list(hits.values())


def _read_metadata_filter(entry):
    """One filter entry as ``(kind, operator, value)``."""
    if isinstance(entry, tuple) and len(entry) == 3:
        return entry
    if isinstance(entry, tuple) and len(entry) == 2:
        kind, bounds = entry
        low = bounds[0] if isinstance(bounds, tuple) else ""
        high = bounds[1] if isinstance(bounds, tuple) and len(bounds) > 1 else ""
        return kind, "range", "%s..%s" % (low, high)
    return None, "", ""


def _metadata_matches(engine, kind, operator, value):
    """``[(doc_id, why)]`` for one metadata filter."""
    kind = str(kind or "").lower()
    value = str(value or "").strip()
    if not value and kind not in ("archive",):
        return []
    results = []
    if kind in ("hash", "checksum", "metadata", "any"):
        for column in ("checksum", "source_path"):
            if kind not in ("metadata", "any") and column != "checksum":
                continue
            for doc_id, why in _column_matches(engine, column, operator, value):
                results.append((doc_id, why))
    if kind in ("path", "source", "metadata", "any"):
        for doc_id, why in _column_matches(engine, "source_path", operator,
                                           value):
            results.append((doc_id, why))
    if kind in ("size", "source_size", "metadata", "any"):
        column = "source_size" if kind == "source_size" else "size"
        for doc_id, why in _column_matches(engine, column, operator, value):
            results.append((doc_id, why))
    if kind in ("mtime", "modified", "metadata", "any"):
        for doc_id, why in _column_matches(engine, "source_mtime", operator,
                                           value):
            results.append((doc_id, why))
    if kind in ("metadata", "any"):
        for doc_id in _all_doc_ids(engine):
            for facet_kind, values in _meta_facets(engine, doc_id).items():
                for facet_value in values:
                    if value.lower() in facet_value.lower():
                        results.append((doc_id, "%s=%s" % (facet_kind,
                                                           facet_value)))
    # Any kind not named above is treated as a meta key, so a document whose
    # metadata carries `language: pl` is found by `language:pl` without this
    # module having to know that key exists.
    if kind not in ("hash", "checksum", "path", "source", "size",
                    "source_size", "mtime", "modified", "metadata", "any"):
        ids = _facet_doc_ids(engine, "meta:" + kind, value, contains=True) or []
        results.extend((doc_id, "%s=%s" % (kind, value)) for doc_id in ids)
    return results


def _column_matches(engine, column, operator, value):
    """Rows whose indexed column matches, numerically where that applies."""
    if column not in ("checksum", "source_path", "size", "source_size",
                      "source_mtime", "updated", "title"):
        return []
    number = _as_number(value)
    try:
        if operator in (">", ">=", "<", "<=") and number is not None:
            rows = engine.conn.execute(
                "SELECT doc_id, %s AS value FROM docs_index "
                "WHERE %s %s ?" % (column, column, operator),
                (number,)).fetchall()
            return [(row["doc_id"], "%s %s %s" % (column, operator, value))
                    for row in rows]
        if ".." in value:
            low, _sep, high = value.partition("..")
            low_number, high_number = _as_number(low), _as_number(high)
            if low_number is not None and high_number is not None:
                rows = engine.conn.execute(
                    "SELECT doc_id FROM docs_index WHERE %s >= ? AND %s <= ?"
                    % (column, column), (low_number, high_number)).fetchall()
                return [(row["doc_id"], "%s in %s..%s" % (column, low, high))
                        for row in rows]
        rows = engine.conn.execute(
            "SELECT doc_id FROM docs_index WHERE lower(%s) LIKE ?" % column,
            ("%" + value.lower() + "%",)).fetchall()
        return [(row["doc_id"], "%s contains %s" % (column, value))
                for row in rows]
    except Exception:
        return []


def _meta_facets(engine, doc_id):
    """``{meta key: [values]}`` for one document."""
    try:
        rows = engine.conn.execute(
            "SELECT facet, value FROM docs_meta_index "
            "WHERE doc_id = ? AND facet LIKE 'meta:%'", (doc_id,)).fetchall()
    except Exception:
        return {}
    out = {}
    for row in rows:
        kind = str(_row_get(row, "facet"))[5:]
        out.setdefault(kind, []).append(str(_row_get(row, "value")))
    return out


# --- structure: sections and headings ---------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BANNER_RE = re.compile(r"^(=+|-{3,}|\*{3,})\s*(.*?)\s*$")
_SECTION_ID_RE = re.compile(r"^([A-Za-z0-9_.\-]+)[\):]\s*(.*)$")


def document_outline(text):
    """A document's headings and sections, in order:
    ``[{kind, level, title, line, text}]``.

    Two spellings are recognised, because the documents in the repository use
    both: Markdown headings (``## Recovery``) and the banner / identifier style
    a plain text document tends to use (``Recovery`` over ``=======``, or
    ``1.2) Installing``). A section owns the lines under it until the next
    heading of the same or a higher level, which is what makes a section
    *searchable* rather than merely listable.
    """
    lines = str(text or "").splitlines()
    outline = []
    for number, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        match = _HEADING_RE.match(line.strip())
        if match:
            outline.append({"kind": "heading", "level": len(match.group(1)),
                            "title": match.group(2).strip(), "line": number,
                            "text": ""})
            continue
        identifier = _SECTION_ID_RE.match(line.strip())
        if identifier and len(identifier.group(1)) <= 12:
            # `2.1) Installing` -- an identifier, then the section's name.
            outline.append({"kind": "section", "level": 2,
                            "title": identifier.group(2).strip()
                            or identifier.group(1),
                            "line": number, "text": ""})
            continue
        banner = _BANNER_RE.match(line.strip())
        if banner and len(line.strip()) >= 3:
            # A banner underlines the title above it, which is how a plain text
            # document says "this is a section". The title is the previous
            # non-blank line, which is *not* necessarily the previous line (a
            # blank line between the title and its underline is common), and at
            # the very top of a document there is no outline entry yet -- so
            # the title is read from the source rather than from the outline.
            title = _title_before(lines, number)
            if title:
                outline.append({"kind": "section", "level": 2,
                                "title": title, "line": number - 1,
                                "text": ""})
            elif banner.group(2):
                outline.append({"kind": "section", "level": 2,
                                "title": banner.group(2), "line": number,
                                "text": ""})
    _attach_section_text(outline, lines)
    return outline


def _title_before(lines, number):
    """The title a banner on line `number` underlines (or "").

    The previous non-blank line is the title. It is looked up in the source
    rather than in the outline because a banner may underline the very first
    line of a document, where no outline entry exists yet, and because a blank
    line between the title and its underline is common enough to matter.
    """
    for index in range(number - 2, -1, -1):
        text = lines[index].strip()
        if not text:
            continue
        # A line that is itself a banner is not a title.
        if _BANNER_RE.match(text) or _HEADING_RE.match(text):
            return ""
        return text
    return ""


def _attach_section_text(outline, lines):
    """Give every entry the body beneath it, up to the next same-level entry."""
    for index, entry in enumerate(outline):
        end = len(lines)
        for other in outline[index + 1:]:
            if other["level"] <= entry["level"]:
                end = other["line"] - 1
                break
        body = "\n".join(lines[entry["line"]:end]).strip()
        entry["text"] = body[:4000]


def _search_heading(engine, spec, documents, limit):
    wanted = " ".join(spec.get("terms") or []) or _first_value(spec, "heading")
    if not wanted:
        return []
    return _outline_matches(engine, documents, wanted, kinds=("heading",),
                            score_key="heading")


def _search_section(engine, spec, documents, limit):
    wanted = (" ".join(spec.get("terms") or [])
              or _first_value(spec, "section"))
    if not wanted:
        return []
    return _outline_matches(engine, documents, wanted,
                            kinds=("section", "heading"), score_key="section")


def _outline_matches(engine, documents, wanted, kinds, score_key):
    """Documents whose outline holds a matching heading or section."""
    wanted = str(wanted or "").strip().lower()
    if not wanted or not documents:
        return []
    hits = []
    for doc in documents:
        doc_id = doc.get("id")
        outline = document_outline(doc.get("body") or "")
        for entry in outline:
            if entry["kind"] not in kinds:
                continue
            title = entry["title"].lower()
            # A section is matched by its own name and by the words inside it,
            # because a reader who knows the phrase but not the heading should
            # still find it.
            if wanted in title:
                hits.append(_result(doc_id, 1.0 - entry["level"] / 100.0,
                                    "%s: %s (line %d)"
                                    % (score_key, entry["title"],
                                       entry["line"]), score_key))
                break
            if wanted in entry["text"].lower():
                hits.append(_result(doc_id, 0.8,
                                    "%s text: %s (line %d)"
                                    % (score_key, entry["title"],
                                       entry["line"]), score_key))
                break
    return hits


def _first_value(spec, kind):
    """The first filter value of a kind (or None)."""
    for filter_kind, _operator, value in spec.get("filters", []):
        if filter_kind == kind:
            return value
    return None


def _first_filter(spec, kind):
    """The first ``(operator, value)`` of a kind (or ``(None, None)``)."""
    for filter_kind, operator, value in spec.get("filters", []):
        if filter_kind == kind:
            return operator, value
    if kind in spec.get("ranges", {}):
        low, high = spec["ranges"][kind]
        return "range", "%s..%s" % (low, high)
    return None, None


_RUNNERS = {
    "content": _search_content,
    "section": _search_section,
    "heading": _search_heading,
    "tag": _search_tag,
    "type": _search_type,
    "date": _search_date,
    "author": _search_author,
    "version": _search_version,
    "history": _search_history,
    "archive": _search_archive,
    "metadata": _search_metadata,
    "fuzzy": _search_fuzzy,
    "semantic": _search_semantic,
}


# ---------------------------------------------------------------------------
# Describing the surface
# ---------------------------------------------------------------------------
def format_results(hits, documents=None, limit=40):
    """Search hits as ASCII lines."""
    titles = {}
    for doc in documents or ():
        if isinstance(doc, dict):
            titles[doc.get("id")] = doc.get("title", "")
    lines = ["Advanced search results", "=======================",
             "hits: %d" % len(hits or [])]
    for hit in (hits or [])[:limit]:
        lines.append("%-10s %-18s %-8.3f %s" % (
            hit.get("kind", "?"), titles.get(hit.get("doc_id"),
                                             str(hit.get("doc_id")))[:18],
            hit.get("score", 0.0), hit.get("why", "")))
    if hits and len(hits) > limit:
        lines.append("... and %d more" % (len(hits) - limit))
    return lines


def format_query(spec):
    """A parsed query as ASCII lines, so a search explains itself."""
    lines = ["Parsed query", "============",
             "kind   : %s" % spec.get("kind", "-"),
             "terms  : %s" % (", ".join(spec.get("terms") or []) or "-")]
    filters = spec.get("filters") or []
    if filters:
        lines.append("filters:")
        for kind, operator, value in filters:
            lines.append("  %-10s %s %s" % (kind, operator, value))
    ranges = spec.get("ranges") or {}
    if ranges:
        lines.append("ranges :")
        for kind, (low, high) in sorted(ranges.items()):
            lines.append("  %-10s %s .. %s" % (kind, low, high))
    if spec.get("raw"):
        lines.append("raw    : %s" % spec["raw"])
    return lines


def search_capabilities():
    """Describe the advanced search for the audit / about layer."""
    return {
        "kinds": list(SEARCH_KIND_KEYS),
        "kind_labels": dict(SEARCH_KIND_LABELS),
        "kind_examples": dict(SEARCH_KIND_EXAMPLES),
        "operators": list(_RANGE_OPERATORS),
        "fuzzy": {
            "algorithm": "levenshtein",
            "max_edits": {3: 1, 5: 1, 8: 2, 999: 3},
            "min_length": FUZZY_MIN_LENGTH,
            "candidates": FUZZY_CANDIDATES,
        },
        "semantic": {
            "algorithm": "cosine over corpus co-occurrence",
            "needs_model": False,
            "needs_network": False,
            "floor": SEMANTIC_FLOOR,
        },
        "structure": {"headings": True, "sections": True,
                      "styles": ["markdown", "banner", "identifier"]},
        "offline": True,
        "ascii_only": True,
        "zero_dependency": True,
        "cross_platform": True,
    }
