"""Nickname <-> canonical first-name table.

Used by the wrong-person audit (and any other identity check that compares
uploaded first names against enriched first names) to avoid flagging things
like "Matt" vs "Matthew" or "Bob" vs "Robert" as mismatches.

Design:
  - A single curated list of equivalence groups. Each group is a set of
    first names that all canonicalize to the same "canonical" name (the
    longest / formal one by convention, but what matters is that they're
    all in the same bucket).
  - `canonical_first_names("Matt")` returns the full equivalence set
    including "matt" itself — i.e. the set of names a "Matt" could also
    go by. The caller expands both uploaded and enriched names through
    this function and treats any non-empty intersection as a match.
  - Some nicknames are genuinely ambiguous across genders or roots
    (Sam -> Samuel or Samantha; Alex -> Alexander or Alexandra;
    Chris -> Christopher or Christina; Pat -> Patrick or Patricia).
    We include ALL plausible canonical forms in the same group. That's
    fine for a "is this probably not a wrong person" check — it only
    makes the audit slightly more permissive, never wrong-direction.
  - Comparison is case-insensitive and whitespace-insensitive; apostrophes
    and accents are ignored.

Scope: ~100 common English-first-name groups, plus a few common variants
from other traditions (Mohammad/Mohammed/Mo, Abhi/Abhishek, Raj/Rajiv,
etc.). Not exhaustive — just the long tail of mismatches we actually see
in practice on Allan's dataset.
"""

from __future__ import annotations

import unicodedata


# Each row is an equivalence class of first names. Order within a row is
# irrelevant. All entries should be lowercase ASCII (no accents, no dots).
_NICKNAME_GROUPS: list[set[str]] = [
    # ── Male, common Anglo ─────────────────────────────────────────────
    {"matt", "matthew", "matty"},
    {"rob", "robbie", "bob", "bobby", "robert"},
    {"mike", "mikey", "michael"},
    {"chris", "christopher", "christina", "christine", "cristina", "kristina", "kristen", "kristin", "kit", "topher"},
    {"dave", "davey", "david"},
    {"dan", "danny", "daniel"},
    {"jim", "jimmy", "jamie", "james"},
    {"joe", "joey", "joseph"},
    {"tom", "tommy", "thomas"},
    {"tim", "timmy", "timothy"},
    {"will", "willy", "willie", "bill", "billy", "william", "liam"},
    {"steve", "stevie", "steven", "stephen"},
    {"mark", "marc", "marcus"},
    {"nick", "nicky", "nicholas", "nikolai", "nicolas"},
    {"tony", "anthony", "antonio"},
    {"ed", "eddie", "eddy", "edward", "ted", "teddy"},
    {"ted", "teddy", "theodore", "edward", "theo"},
    {"rick", "ricky", "dick", "dickie", "richard"},
    {"sam", "sammy", "samuel", "samantha"},
    {"alex", "alexander", "alexandra", "alexis", "alec", "aleks", "al"},
    {"andy", "drew", "andrew"},
    {"ben", "benny", "benjamin", "benjy", "ben", "benji"},
    {"charlie", "chuck", "chas", "chaz", "charles"},
    {"fred", "freddy", "freddie", "frederick", "alfred"},
    {"greg", "gregg", "gregory"},
    {"jack", "john", "johnny", "jackie", "jonathan", "jon"},
    {"jeff", "jeffrey", "geoff", "geoffrey"},
    {"joel", "joely"},
    {"josh", "joshua"},
    {"ken", "kenny", "kenneth"},
    {"larry", "lawrence", "laurence"},
    {"len", "lenny", "leonard", "leo", "lennart"},
    {"lou", "louie", "louis", "lewis"},
    {"nate", "nathan", "nathaniel"},
    {"pat", "patrick", "patricia", "patty", "patti", "paddy"},
    {"pete", "peter", "petey"},
    {"phil", "philip", "phillip"},
    {"ron", "ronny", "ronnie", "ronald"},
    {"russ", "russell", "rusty"},
    {"stan", "stanley"},
    {"vince", "vinny", "vincent"},
    {"walt", "walter"},
    {"zach", "zack", "zak", "zachary", "zachariah"},
    {"tony", "anthony"},
    {"ollie", "oliver"},
    {"gabe", "gabriel"},
    {"gus", "augustus", "august"},
    {"hal", "harry", "harold", "henry", "hank"},
    {"art", "arty", "arthur"},
    {"sol", "solomon"},
    {"abe", "abraham", "abram"},
    {"leo", "leonardo", "leonard"},
    {"doug", "douglas"},
    {"mitch", "mitchell"},
    {"cal", "calvin"},

    # ── Female, common Anglo ───────────────────────────────────────────
    {"kate", "katie", "kat", "katherine", "kathryn", "katharine", "catherine", "cathy", "cate", "kathy", "kathleen", "katelyn", "caitlyn", "caitlin", "kaitlin"},
    {"jen", "jenny", "jennie", "jennifer", "ginny"},
    {"beth", "bethany", "betty", "betsy", "liz", "lizzy", "lizzie", "eliza", "elizabeth", "libby", "ellie"},
    {"sue", "susie", "suzy", "susan", "susanna", "susannah"},
    {"maggie", "meg", "peggy", "margie", "margo", "margot", "margaret", "marge"},
    {"jess", "jessie", "jessica"},
    {"becky", "becca", "rebecca", "rebekah"},
    {"abby", "abbie", "abigail"},
    {"amy", "amelia", "emilia"},
    {"em", "emmy", "emma", "emily", "emmeline"},
    {"nat", "natty", "natalie", "natasha", "natalia", "nataniel"},
    {"sophie", "sophia", "sofia"},
    {"nikki", "nicki", "nicole", "nichole"},
    {"pam", "pammy", "pamela"},
    {"deb", "debbie", "deborah", "debra"},
    {"carol", "caroline", "carolyn", "carrie", "cari"},
    {"vicky", "vickie", "victoria"},
    {"angie", "angela"},
    {"annie", "anna", "ann", "anne", "hannah"},
    {"mandy", "amanda"},
    {"mel", "melissa", "melanie", "mellie"},
    {"tracy", "tracey"},
    {"terry", "teresa", "theresa", "terri"},
    {"allie", "ally", "alison", "allison", "alice", "alicia"},
    {"steph", "stephanie"},
    {"jackie", "jacqueline", "jacquelyn"},
    {"jill", "julie", "julia", "juliet", "julian"},
    {"val", "valerie"},
    {"dee", "denise", "deanne", "deanna"},
    {"kim", "kimberly", "kimberley"},
    {"lisa", "liza", "elise", "elisa"},
    {"christie", "christy", "christina"},
    {"laura", "laurie", "lori", "lorraine"},
    {"gabby", "gabi", "gabrielle", "gabriela"},
    {"bea", "beatrice", "beatrix"},
    {"charlotte", "lottie", "charlie"},
    {"fran", "frannie", "frances", "francesca"},

    # ── Mohammad variants (by far the most common cross-culture miss) ──
    {"mo", "mohammad", "mohammed", "muhammad", "mohamad", "mohamed", "mohd"},
    # ── South Asian common nicknames ───────────────────────────────────
    {"abhi", "abhishek", "abhinav", "abhijit"},
    {"raj", "rajiv", "rajesh", "raja", "rajan"},
    {"vik", "vikram", "vikas", "vickram"},
    {"sid", "siddharth", "siddhartha", "sidd"},
    {"sam", "sameer", "samir"},
    {"dev", "devan", "devendra"},
    {"arj", "arjun"},
    # ── East Asian common Anglicizations (people pick a western name) ──
    # Note: intentionally conservative — only include when we've seen these
    # cluster on real data. Add more as they show up.
]


# ── Build index: name -> set of canonical equivalents ──
_CANONICAL_INDEX: dict[str, set[str]] = {}
for _group in _NICKNAME_GROUPS:
    frozen = frozenset(_group)
    for _name in _group:
        # Merge rather than overwrite — one name may end up in multiple groups
        # legitimately (e.g. "sam" is in both {sam, samuel, samantha} and
        # {sam, sameer}). Intentional: caller gets the union.
        existing = _CANONICAL_INDEX.get(_name)
        if existing is None:
            _CANONICAL_INDEX[_name] = set(frozen)
        else:
            existing.update(frozen)


def _normalize(name: str) -> str:
    """Lowercase, strip accents, drop non-letters, collapse whitespace."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower().strip()
    # keep only letters and hyphens (drop apostrophes, dots, etc.)
    out = []
    for ch in n:
        if ch.isalpha() or ch == "-":
            out.append(ch)
    return "".join(out)


def canonical_first_names(name: str) -> set[str]:
    """Return all first-name forms equivalent to the given name.

    If the name isn't in the table, returns {normalized_name} — i.e. the
    caller still gets a single-element set to intersect against, so callers
    don't need to special-case "not in table".

    Examples:
        canonical_first_names("Matt")     -> {"matt", "matthew", "matty"}
        canonical_first_names("Matthew")  -> {"matt", "matthew", "matty"}
        canonical_first_names("Kate")     -> {"kate", "katie", "kat",
                                              "katherine", "kathryn", ...}
        canonical_first_names("Obadiah")  -> {"obadiah"}   # unknown name
        canonical_first_names("")         -> set()
    """
    norm = _normalize(name)
    if not norm:
        return set()
    group = _CANONICAL_INDEX.get(norm)
    if group is not None:
        return set(group)
    return {norm}


def are_nickname_equivalent(a: str, b: str) -> bool:
    """True if the two names could refer to the same canonical first name.

    Uses `canonical_first_names` on both sides and checks for intersection.
    """
    na = canonical_first_names(a)
    nb = canonical_first_names(b)
    if not na or not nb:
        return False
    return bool(na & nb)


__all__ = ["canonical_first_names", "are_nickname_equivalent"]
