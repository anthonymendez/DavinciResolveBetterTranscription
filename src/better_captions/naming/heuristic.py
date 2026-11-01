"""Rule-based speaker naming: self-introductions and vocatives, no network or models."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass

from better_captions.schema import Segment, Speaker

# Names no transcript namer overrides: typed by the user (pinned tracks), or recognised by voice
# (engine.voices, from speakers the user confirmed on earlier videos).
KEPT_SOURCES = ("user", "voice")


def is_kept(speaker: Speaker) -> bool:
    return bool(speaker.name) and speaker.name_source in KEPT_SOURCES


# A capitalised word that could be a name (allows O'Brien, Mary-Jane, José).
_NAME = r"[A-Z][a-zà-ÿ]+(?:['-][A-Z]?[a-zà-ÿ]+)?"
_FULL_NAME = rf"{_NAME}(?:\s+{_NAME})?"

# (pattern, score). The trigger is case-insensitive, the name must be capitalised.
_SELF_INTRO = [
    (re.compile(rf"\b(?i:my\s+name\s+is|my\s+name's|name's)\s+({_FULL_NAME})"), 1.0),
    (re.compile(rf"\b(?i:call\s+me)\s+({_NAME})"), 0.8),
    (re.compile(rf"\b(?i:i'm|i\s+am|im)\s+({_FULL_NAME})"), 0.8),
    (re.compile(rf"\b(?i:this\s+is|it's)\s+({_FULL_NAME})\s*(?:here\b|speaking\b|[,.!]|$)"), 0.5),
]

# Words that commonly precede a vocative: "Hey Jordan", "thanks Maria".
_GREETING = (
    r"(?i:hey|hi|hello|hiya|yo|thanks|thank\s+you|cheers|bye|goodbye|see\s+you|sorry|"
    r"morning|good\s+morning|good\s+night|night|welcome|welcome\s+back|congrats|"
    r"please|okay|ok|well|listen|look|come\s+on|oh)"
)
_VOCATIVE = [
    # "Hey Jordan, ..." / "thanks Maria."
    (re.compile(rf"\b{_GREETING},?\s+({_NAME})\b(?!\s+{_NAME})"), 1.0),
    # "Jordan, how are you?" at the start of a sentence.
    (re.compile(rf"(?:^|[.!?]\s+)({_NAME}),\s"), 0.8),
    # "..., Maria." / "..., Maria?" at the end of a clause.
    (re.compile(rf",\s+({_NAME})\s*(?:[.!?]+|$)"), 0.8),
]
_VOCATIVE_WEIGHT = 0.6

# Capitalised words that are not names (sentence starters, fillers, address terms, ...).
_NOT_NAMES = {
    w.lower()
    for w in """
    I I'm I'll I've I'd Im A An The This That These Those It It's Its He She We They You Your
    Yes Yeah Yep Yup No Nope Nah Okay Ok Alright Right Sure Fine Well So Oh Ah Uh Um Hmm Wow
    Hey Hi Hello Hiya Yo Bye Goodbye Thanks Thank Cheers Sorry Please Welcome Congrats
    Good Great Nice Cool Awesome Perfect Amazing Bad Happy Sad Glad Ready Done Back Here There
    Now Then Today Tomorrow Tonight Yesterday Morning Night Evening Afternoon
    Just Really Actually Literally Basically Honestly Maybe Probably Definitely Totally
    Not Going Gonna Wanna Gotta Trying Talking Looking Saying Getting Doing Coming Leaving
    Wait Look Listen Come Go Stop Let Lets Let's What Why How When Where Who Which Whatever
    And But Or If Because Also Anyway Like Even Still Again
    Guys Guy Everyone Everybody Anyone Someone Nobody Folks People Team Man Dude Bro Bud Buddy
    Mate Pal Sir Madam Ma'am Miss Mister Mr Mrs Ms Dr Boss Chief Kid Kids Son Honey Babe Baby
    Sweetie Darling Love Mom Mum Dad Mommy Daddy Grandma Grandpa Brother Sister Friend Friends
    God Jesus Christ Lord Gosh
    Monday Tuesday Wednesday Thursday Friday Saturday Sunday
    January February March April May June July August September October November December
    English Spanish French German American British Internet Youtube TV
    One Two Three Four Five Ten Hundred First Second Last Next
    """.split()
}


@dataclass
class _Evidence:
    score: float = 0.0
    display: str = ""


def _clean(name: str) -> str | None:
    """Drop non-name words; return the remaining name or None."""
    tokens = [t for t in name.split() if t.lower().strip("'") not in _NOT_NAMES]
    # Keep a two-word name only if both words survived and it started with a name.
    if not tokens or tokens[0] != name.split()[0]:
        return None
    return " ".join(tokens)


def _key(name: str) -> str:
    """Names are matched on their first word, so "Maria" and "Maria Lopez" are the same person."""
    return name.split()[0].lower()


def _turns(segments: list[Segment]) -> list[tuple[str, str]]:
    """Merge consecutive same-speaker segments into (speaker, text) turns."""
    turns: list[tuple[str, str]] = []
    for seg in segments:
        if turns and turns[-1][0] == seg.speaker:
            turns[-1] = (seg.speaker, f"{turns[-1][1]} {seg.text}")
        else:
            turns.append((seg.speaker, seg.text))
    return turns


def _neighbours(turns: list[tuple[str, str]], i: int) -> list[str]:
    """The previous and next *other* speakers around turn i (deduplicated)."""
    me = turns[i][0]
    prev = next((s for s, _ in reversed(turns[:i]) if s != me), None)
    nxt = next((s for s, _ in turns[i + 1 :] if s != me), None)
    return list(dict.fromkeys(s for s in (nxt, prev) if s is not None))


def score_candidates(segments: list[Segment]) -> dict[str, dict[str, _Evidence]]:
    """speaker id -> name key -> accumulated evidence."""
    scores: dict[str, dict[str, _Evidence]] = defaultdict(lambda: defaultdict(_Evidence))
    # Names a speaker uses to address someone else are not their own.
    not_me: dict[str, set[str]] = defaultdict(set)

    def credit(speaker: str, name: str, amount: float) -> None:
        ev = scores[speaker][_key(name)]
        ev.score += amount
        if len(name) > len(ev.display):
            ev.display = name

    turns = _turns(segments)
    for i, (speaker, text) in enumerate(turns):
        for pattern, weight in _SELF_INTRO:
            for m in pattern.finditer(text):
                if name := _clean(m.group(1)):
                    credit(speaker, name, weight)
        addressees = _neighbours(turns, i)
        seen: set[str] = set()
        for pattern, weight in _VOCATIVE:
            for m in pattern.finditer(text):
                name = _clean(m.group(1))
                if not name or _key(name) in seen:
                    continue
                seen.add(_key(name))
                not_me[speaker].add(_key(name))
                for addressee in addressees:
                    credit(addressee, name, _VOCATIVE_WEIGHT * weight / len(addressees))

    for speaker, keys in not_me.items():
        for key in keys:
            scores[speaker].pop(key, None)
    return scores


def _confidence(best: float, total: float) -> float:
    # Saturating in the amount of evidence, discounted by competing candidates.
    return round(min(0.95, (1 - math.exp(-1.5 * best)) * best / total), 3)


def name_speakers(speakers: list[Speaker], segments: list[Segment], min_score: float = 0.25) -> list[Speaker]:
    """Fill name/name_confidence/name_source="heuristic" on copies of speakers where evidence allows.

    Speakers already named by the user (or recognised by voice, see KEPT_SOURCES) are left alone and their
    names are not reused.
    """
    scores = score_candidates(segments)
    taken = {_key(s.name) for s in speakers if is_kept(s)}
    open_ids = {s.id for s in speakers if not is_kept(s)}

    candidates = [
        (ev.score, sid, key, ev.display)
        for sid, by_name in scores.items()
        if sid in open_ids
        for key, ev in by_name.items()
        if ev.score >= min_score
    ]
    # Greedy best-first: each speaker gets at most one name, each name at most one speaker.
    assigned: dict[str, tuple[str, float]] = {}
    for score, sid, key, display in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if sid in assigned or key in taken:
            continue
        total = sum(ev.score for ev in scores[sid].values())
        assigned[sid] = (display, _confidence(score, total))
        taken.add(key)

    out = []
    for s in speakers:
        if s.id in assigned:
            name, conf = assigned[s.id]
            out.append(Speaker(id=s.id, name=name, name_confidence=conf, name_source="heuristic"))
        elif s.id in open_ids:
            out.append(Speaker(id=s.id))
        else:
            out.append(Speaker(id=s.id, name=s.name, name_confidence=s.name_confidence, name_source=s.name_source))
    return out
