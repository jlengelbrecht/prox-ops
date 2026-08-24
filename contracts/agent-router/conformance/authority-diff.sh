#!/usr/bin/env bash
# Classify the change between two agent-router catalog documents as
# authority-EXPANDING, authority-NARROWING or NEUTRAL.
#
#   authority-diff.sh <old-catalog.yaml> <new-catalog.yaml>
#
# Prints one line `verdict: expanding|narrowing|neutral` and exits 0. A
# non-zero exit is a TOOL error (bad usage, unreadable or unparsable input)
# and never a reached verdict - a caller that treats "it failed" as "nothing
# changed" would be reading a crash as an approval.
#
# WHY THIS EXISTS
#
# The catalog is the Git-managed half of the rule the contract freezes as
#
#     effective capability = Git/catalog authority INTERSECT heartbeat observation
#
# Everything the router may do is bounded by this document, so the question a
# reviewer of a catalog PR actually has is not "what changed" - `git diff`
# answers that - but "did this PR hand out authority that was not there
# before". That question is answerable mechanically, and a diff that only a
# human answers is a diff that gets waved through at 23:00.
#
# THE TAXONOMY IS DOCUMENTED IN FULL IN AUTHORITY-DIFF.md NEXT TO THIS FILE.
# The summary, because a reader of this script should not have to leave it:
#
#   Authority(C) is the set of execution attempts catalog C admits. The change
#   C_old -> C_new is EXPANDING if it admits an attempt C_old did not, and
#   NARROWING if it withdraws one and admits none. Proving set inclusion over
#   the whole document is not tractable, so the change is decomposed into
#   ATOMS - one per changed leaf, per changed collection membership, per added
#   or removed table entry, and per added or removed UNRECOGNIZED key - each
#   atom is classified by the rule family its field belongs to, and the atoms
#   are combined by DOMINANCE: any expanding atom makes the whole change
#   expanding, else any narrowing atom makes it narrowing, else neutral.
#
#   Rule families, all of them declared in the tables below:
#     documentary   prose and provenance - carries no authority          neutral
#     gate          selectable/supported/allow_cold_start, status  true admits
#     closed value  spillover/router_behaviour        one value is the closed one
#     restriction   forbidden_for/blocked_by/requires        members subtract
#     grant         capabilities/physical/entitlements/...       members add
#     claim         min_context/max_context/vram_gb/...     more is more
#     constraint    min_vram_gb/min_free_vram_gb            more is less
#     lattice       cost_class/alignment                  ranked permissiveness
#     order         a pure permutation of any list                      neutral
#     everything else                          conservative default: EXPANDING
#
#   The conservative default covers a key's PRESENCE as well as its value: an
#   unrecognized key arriving or departing is an atom in its own right, so an
#   added `experimental_grant: {}` cannot walk to zero atoms and read neutral.
#
#   Two absence rules do the work that `null` does in this catalog, where an
#   absent key and an explicit null mean the same thing:
#     - absence of a CLAIM grants nothing (null min_context guarantees nothing);
#     - absence of a RESTRICTION restricts nothing (null min_vram_gb is no floor).
#
#   Adding or removing a whole TABLE ENTRY moves authority only if the entry
#   ADMITS, and it admits only if every entry-level gate it declares says yes -
#   `selectable`, `supported` and `status`. A `status: planned` entry is
#   non-admitting however `selectable` it claims to be, so naming it is neutral
#   in both directions. Authority moves at the flip, not at the name.
#
# The tool reads two files and nothing else: no network, no cluster, no
# credential, and no dependency on the live catalog. Point it at any two
# catalog documents - or at the ConfigMap that ships one, which it unwraps.
#
# Style note: python3 + PyYAML, like verify-placement-cases.sh and
# verify-ade-boundary.sh beside it. Deliberately no `cd` - the arguments are
# resolved against the CALLER's working directory, because a checker that
# silently reinterprets a relative path is a checker that checks the wrong file.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: authority-diff.sh <old-catalog.yaml> <new-catalog.yaml>" >&2
  exit 2
fi

for f in "$1" "$2"; do
  if [[ ! -r "$f" ]]; then
    echo "authority-diff: cannot read '$f'" >&2
    exit 2
  fi
done

python3 - "$1" "$2" <<'PY'
import json
import sys

try:
    import yaml
except ImportError:
    sys.exit("authority-diff: PyYAML is required (this tool parses catalog "
             "documents). Install it - 'pip install pyyaml' - rather than "
             "skipping: a classifier that cannot parse must fail, not guess.")

EXPANDING, NARROWING, NEUTRAL = "expanding", "narrowing", "neutral"

# --- rule tables ------------------------------------------------------------
#
# Every table below is keyed on a LEAF FIELD NAME, not on a full path, so a
# field means the same thing wherever it appears. `cost_class` on a pool, on a
# profile and on an entitlement candidate is one concept and gets one rule;
# splitting it per table is how two copies of one concept quietly drift apart.
# It also means the classifier works on a fragment - a two-line document
# carrying one `selectable:` is classified by the same rule as the full catalog.

# Prose, provenance and scheduling hints. None of these decides whether an
# attempt is admitted, so a change to one moves no authority. Being wrong in
# the permissive direction here is the one place this classifier can hide an
# expansion, which is why the list is closed and short rather than a pattern.
DOC_KEYS = {
    "consumers", "description", "epic", "notes", "reason", "runtime",
    "schema_version", "source", "story", "updated", "upstream_id_form",
    "version",
    # Operational and scheduling facts: they change which admitted candidate
    # wins, never which candidates are admitted. Same reason the ordering of
    # `prefer_order` is neutral below.
    "idle_retention_min", "preemptible", "resolves_today", "scale_to_zero",
    "vram_gb_nameplate", "warm_bonus_rank_shift", "warm_preference",
}
DOC_SUFFIXES = ("_estimate", "_evidence", "_notes", "_source")
DOC_SUBTREES = {"gpu"}          # hardware description, never a routing input

# Booleans that gate participation. Only `true` admits: false, null and absent
# are the same answer, which is what "an absent key and an explicit null mean
# the same thing" costs a consumer here.
GATE_BOOLS = {"allow_cold_start", "selectable", "supported"}
ADMITTING_STATUS = {"available"}

# The gates that decide whether a whole TABLE ENTRY admits - see admits().
# `allow_cold_start` is deliberately NOT one of them: it is a per-candidate
# condition inside a policy that still admits every warm candidate, not a
# switch that takes the entry out of play.
ENTRY_GATE_BOOLS = ("selectable", "supported")

# Fields with exactly one CLOSED value, where every other value - and an absent
# key, since an undeclared restriction restricts nothing - is the open one.
CLOSED_VALUES = {
    "spillover": "none",                     # invariant 4, written as data
    "router_behaviour": "refuse-to-emit",    # the reserved-harness refusal
}

RESTRICTION_LISTS = {"blocked_by", "forbidden_for", "requires"}
GRANT_LISTS = {"capabilities", "placements", "prefer_order"}
GRANT_RECORD_LISTS = {                      # list -> the fields identifying a member
    "entitlements": ("pool",),
    "physical": ("model_id", "placement"),
}

CLAIM_NUMBERS = {"concurrent_models", "max_context", "min_context", "vram_gb"}
CONSTRAINT_NUMBERS = {"min_free_vram_gb", "min_vram_gb"}

# (rank map, rank of null/absent). Absent is ranked at the PERMISSIVE end for
# both: an undeclared restriction restricts nothing.
LATTICES = {
    "alignment": ({"standard": 0, "unrestricted": 1}, 1),
    "cost_class": ({"metered": 0, "subscription": 1, "free": 2}, 2),
}

# Mappings whose keys are entries in an authority table rather than fields.
TABLES = {
    ("entitlement_pools",), ("harnesses",), ("models",), ("placements",),
    ("policies", "placement_policy"), ("profiles",),
}
KNOWN_TABLES = {"entitlement_pools", "harnesses", "models", "placements",
                "policies", "profiles"}

# Names this document is BUILT OUT OF, rather than fields carrying a rule of
# their own. Listed only so the unrecognized-KEY rule in diff_mapping fires on
# a genuinely novel name and never on the document's own skeleton.
STRUCTURAL_KEYS = {"capacity", "defaults", "placement_policy"}

# Every key name some rule above claims. A key outside this set is surface
# nobody has classified, so its ARRIVAL or DEPARTURE is an atom in its own
# right - otherwise an added `experimental_grant: {}` would walk to no atoms at
# all and come back neutral, which is the conservative default failing open on
# exactly the shape it exists for.
KNOWN_KEYS = (DOC_KEYS | GATE_BOOLS | {"status"} | set(CLOSED_VALUES)
              | RESTRICTION_LISTS | GRANT_LISTS | set(GRANT_RECORD_LISTS)
              | CLAIM_NUMBERS | CONSTRAINT_NUMBERS | set(LATTICES)
              | KNOWN_TABLES | STRUCTURAL_KEYS)


# --- input ------------------------------------------------------------------

def die(msg):
    sys.stderr.write("authority-diff: %s\n" % msg)
    raise SystemExit(1)


def unwrap(doc):
    """A catalog document, whether handed over bare or still inside the
    ConfigMap that ships it. The live copy is ConfigMap-wrapped and a reviewer
    will paste whichever one is to hand; the envelope is not catalog data and
    must not show up in the verdict."""
    if isinstance(doc, dict):
        data = doc.get("data")
        if isinstance(data, dict) and isinstance(data.get("catalog.yaml"), str):
            return yaml.safe_load(data["catalog.yaml"])
    return doc


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            docs = [d for d in yaml.safe_load_all(handle) if d is not None]
    except OSError as exc:
        die("cannot read %s: %s" % (path, exc))
    except yaml.YAMLError as exc:
        die("cannot parse %s as YAML: %s" % (path, exc))
    if not docs:
        die("%s contains no YAML document" % path)
    first = None
    for doc in docs:
        try:
            catalog = unwrap(doc)
        except yaml.YAMLError as exc:
            die("cannot parse the catalog.yaml embedded in %s: %s" % (path, exc))
        if isinstance(catalog, dict) and KNOWN_TABLES & set(catalog):
            return catalog
        if first is None:
            first = catalog
    if not isinstance(first, dict):
        die("%s is not a catalog document (expected a YAML mapping at the root)"
            % path)
    return first


# --- helpers ----------------------------------------------------------------

def fmt(value):
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


def canon(item):
    """A stable identity string for a list member. Falls back to repr rather
    than raising: an exotic value must not turn a verdict into a crash."""
    try:
        return json.dumps(item, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(item)


def number(value):
    """The rank of a numeric field. null/absent is the bottom of the scale for
    both numeric families - see the two absence rules in the header."""
    if value is None:
        return float("-inf")
    if isinstance(value, bool):         # bool is an int in Python; not a measurement
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def is_doc(path):
    if any(seg in DOC_SUBTREES for seg in path):
        return True
    key = str(path[-1])
    return key in DOC_KEYS or key.endswith(DOC_SUFFIXES)


def recognized(path):
    """Does some rule table name this key, or is it documentary? Consulted only
    to decide whether the key's PRESENCE is an atom. A key present on both
    sides is always classified by its value, recognized or not."""
    return is_doc(path) or str(path[-1]) in KNOWN_KEYS


def emit(atoms, verdict, path, detail, why):
    atoms.append((verdict, ".".join(str(p) for p in path), detail, why))


# --- the walk ---------------------------------------------------------------

def diff(path, old, new, atoms):
    if old == new:
        return
    if path and is_doc(path):
        emit(atoms, NEUTRAL, path, "%s -> %s" % (fmt(old), fmt(new)),
             "documentary: describes, does not admit")
        return

    if isinstance(old, dict) or isinstance(new, dict):
        o = old if isinstance(old, dict) else ({} if old is None else None)
        n = new if isinstance(new, dict) else ({} if new is None else None)
        if o is None or n is None:
            emit(atoms, EXPANDING, path, "%s -> %s" % (fmt(old), fmt(new)),
                 "re-typed field: unclassifiable, assumed expanding")
            return
        diff_mapping(path, o, n, atoms)
        return

    if isinstance(old, list) or isinstance(new, list):
        o = old if isinstance(old, list) else ([] if old is None else None)
        n = new if isinstance(new, list) else ([] if new is None else None)
        if o is None or n is None:
            emit(atoms, EXPANDING, path, "%s -> %s" % (fmt(old), fmt(new)),
                 "re-typed field: unclassifiable, assumed expanding")
            return
        diff_list(path, o, n, atoms)
        return

    diff_scalar(path, old, new, atoms)


def diff_mapping(path, old, new, atoms):
    table = tuple(path) in TABLES
    for key in sorted(set(old) | set(new), key=str):
        sub = path + (key,)
        if key in old and key in new:
            diff(sub, old[key], new[key], atoms)
        elif table:
            entry(sub, new[key] if key in new else old[key], atoms, key in new)
        elif not recognized(sub):
            # The KEY itself is the unrecognized surface, independently of what
            # hangs off it. Without this atom an added `experimental_grant: {}`
            # walks to nothing and reads neutral - the conservative default
            # failing open on the one shape where the field says least about
            # itself. Not recursed into: the verdict is already the most
            # conservative one available, and the contents of a field nothing
            # understands cannot make it more so.
            emit(atoms, EXPANDING, sub,
                 "key %s" % ("added" if key in new else "removed"),
                 "unrecognized key: its presence is unclassified surface, "
                 "assumed to carry authority (conservative default)")
        else:
            # Absent and null are the same value in this document, so an added
            # or removed field is diffed against None rather than special-cased.
            diff(sub, old.get(key), new.get(key), atoms)


def admits(record):
    """Does this table entry take part in an approved execution decision?

    Every entry-level gate it declares has to say yes. A harness declares two
    booleans and needs both; a placement declares `status` as well, and
    `status: planned` is non-admitting however `selectable` the entry claims to
    be - a gate that only counts when the other gates agree with it is not a
    gate. An entry declaring no gate at all is assumed to admit, because
    nothing in it says otherwise."""
    if not isinstance(record, dict):
        return True
    for gate in ENTRY_GATE_BOOLS:
        if gate in record and record[gate] is not True:
            return False
    if "status" in record:
        status = record["status"]
        # isinstance first: a non-string status is a malformed gate, and a
        # malformed gate is not an admitting one. It also keeps an unhashable
        # value out of the set membership test.
        if not (isinstance(status, str) and status in ADMITTING_STATUS):
            return False
    return True


def entry(path, record, atoms, added):
    verb = "added" if added else "removed"
    if admits(record):
        emit(atoms, EXPANDING if added else NARROWING, path,
             "entry %s" % verb,
             "an admitting entry %s the approved set" %
             ("joins" if added else "leaves"))
    else:
        emit(atoms, NEUTRAL, path,
             "entry %s (declared non-admitting)" % verb,
             "an entry whose own gates refuse it grants nothing; authority "
             "moves when a gate is flipped, not when the entry is named")


def index(items, ids):
    """A list as the set its members denote. Identity is the whole member for a
    scalar list, and the declared id fields for a record list - so a reordered
    list is the same set, and a member whose identity changed is a removal plus
    an addition rather than an edit."""
    out = {}
    for item in items:
        if ids and isinstance(item, dict):
            key = " ".join("%s=%s" % (i, fmt(item.get(i))) for i in ids)
        else:
            key = canon(item)
        out.setdefault(key, item)
    return out


def members(path, old, new, atoms, ids, added, removed, why, recurse):
    before, after = [canon(i) for i in old], [canon(i) for i in new]
    if before != after and sorted(before) == sorted(after):
        # Reported rather than passed over in silence: a reordering is a real
        # change a reviewer wants acknowledged, it just is not an authority one.
        emit(atoms, NEUTRAL, path, "reordered",
             "order expresses preference, not permission")
    o, n = index(old, ids), index(new, ids)
    for key in sorted(set(o) | set(n), key=str):
        if key in o and key in n:
            if recurse:
                diff(path + (key,), o[key], n[key], atoms)
        elif key in n:
            emit(atoms, added, path, "+ %s" % key, why)
        else:
            emit(atoms, removed, path, "- %s" % key, why)


def record_id(old, new):
    """The field an unrecognized list of records is keyed by, if there is one."""
    items = list(old) + list(new)
    for candidate in ("name", "id"):
        if items and all(isinstance(i, dict) and candidate in i for i in items):
            return candidate
    return None


def diff_list(path, old, new, atoms):
    key = str(path[-1])
    if key in RESTRICTION_LISTS:
        members(path, old, new, atoms, None, NARROWING, EXPANDING,
                "restriction list: a member subtracts authority", False)
        return
    if key in GRANT_LISTS:
        members(path, old, new, atoms, None, EXPANDING, NARROWING,
                "grant list: a member adds authority", False)
        return
    if key in GRANT_RECORD_LISTS:
        members(path, old, new, atoms, GRANT_RECORD_LISTS[key],
                EXPANDING, NARROWING,
                "grant list: a candidate adds authority", True)
        return

    # Unrecognized list. Membership is unclassifiable - we cannot tell a grant
    # from a restriction - so any membership change is assumed expanding in
    # BOTH directions, while a pure permutation stays neutral because ordering
    # is preference everywhere else in this document.
    ident = record_id(old, new)
    if ident:
        members(path, old, new, atoms, (ident,), EXPANDING, EXPANDING,
                "unrecognized list of records: membership change assumed expanding",
                True)
        return
    if sorted(canon(i) for i in old) == sorted(canon(i) for i in new):
        emit(atoms, NEUTRAL, path, "reordered",
             "order expresses preference, not permission")
        return
    emit(atoms, EXPANDING, path, "%s -> %s" % (fmt(old), fmt(new)),
         "unrecognized list: membership change assumed expanding")


def diff_scalar(path, old, new, atoms):
    key = str(path[-1])
    shown = "%s -> %s" % (fmt(old), fmt(new))

    if key in GATE_BOOLS:
        was, now = old is True, new is True
        if was == now:
            emit(atoms, NEUTRAL, path, shown,
                 "gate flag: only true admits, so this changed nothing")
        else:
            emit(atoms, EXPANDING if now else NARROWING, path, shown,
                 "gate flag: true admits, anything else does not")
        return

    if key == "status":
        was, now = old in ADMITTING_STATUS, new in ADMITTING_STATUS
        if was == now:
            emit(atoms, NEUTRAL, path, shown,
                 "status: neither value is 'available', so nothing was admitted "
                 "or withdrawn")
        else:
            emit(atoms, EXPANDING if now else NARROWING, path, shown,
                 "status: 'available' is the admitting value")
        return

    if key in CLOSED_VALUES:
        shut = CLOSED_VALUES[key]
        was, now = (0 if old == shut else 1), (0 if new == shut else 1)
        if was != now:
            emit(atoms, EXPANDING if now > was else NARROWING, path, shown,
                 "'%s' is the closed value of %s; anything else is open"
                 % (shut, key))
        else:
            # Two different open values. What is on the other side changed, and
            # nothing here can say the new one is the smaller.
            emit(atoms, EXPANDING, path, shown,
                 "%s re-pointed between open values: assumed expanding" % key)
        return

    if key in LATTICES:
        ranks, absent = LATTICES[key]
        a = absent if old is None else ranks.get(old)
        b = absent if new is None else ranks.get(new)
        if a is None or b is None:
            emit(atoms, EXPANDING, path, shown,
                 "unrecognized %s value: assumed expanding" % key)
        elif a == b:
            emit(atoms, NEUTRAL, path, shown, "same rank on the %s lattice" % key)
        else:
            emit(atoms, EXPANDING if b > a else NARROWING, path, shown,
                 "%s lattice: higher is more permissive" % key)
        return

    if key in CLAIM_NUMBERS or key in CONSTRAINT_NUMBERS:
        a, b = number(old), number(new)
        if a is None or b is None:
            emit(atoms, EXPANDING, path, shown,
                 "non-numeric value in a numeric field: assumed expanding")
        elif a == b:
            emit(atoms, NEUTRAL, path, shown, "same value")
        elif key in CLAIM_NUMBERS:
            emit(atoms, EXPANDING if b > a else NARROWING, path, shown,
                 "capability claim: a bigger guarantee satisfies more requests")
        else:
            emit(atoms, NARROWING if b > a else EXPANDING, path, shown,
                 "hard constraint: a higher floor admits fewer candidates")
        return

    emit(atoms, EXPANDING, path, shown,
         "unrecognized field: assumed to add authority (conservative default)")


# --- run --------------------------------------------------------------------

old_path, new_path = sys.argv[1], sys.argv[2]
atoms = []
diff((), load(old_path), load(new_path), atoms)

counts = {EXPANDING: 0, NARROWING: 0, NEUTRAL: 0}
for atom in atoms:
    counts[atom[0]] += 1

if counts[EXPANDING]:
    verdict = EXPANDING
elif counts[NARROWING]:
    verdict = NARROWING
else:
    verdict = NEUTRAL

print("authority-diff: %s -> %s" % (old_path, new_path))
for kind, where, detail, why in atoms:
    print("  %-9s %-46s %-28s [%s]" % (kind, where, detail, why))
print("summary: %d expanding, %d narrowing, %d neutral (%d atom(s))"
      % (counts[EXPANDING], counts[NARROWING], counts[NEUTRAL], len(atoms)))
print("verdict: %s" % verdict)
PY
