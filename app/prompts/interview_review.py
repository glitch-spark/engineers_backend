"""Shared interview-review vocabulary.

Single source of truth for failure-mode names, stage labels, and per-stage
drill recommendations. Used by both per-interview review (ai_review.py) and
cross-interview rollup (interviews.py /analyze) so the two views speak the
same language.
"""

STAGE_LABELS: dict[str, str] = {
    "intro": "Intro",
    "tech": "Tech",
    "panel": "Panel",
    "live_coding": "Live Coding",
    "system_design": "System Design",
    "cultural": "Cultural",
    "final": "Final",
    "ai_interview": "AI Interview",
}

# Named failure-mode catalog. Keep terse — the prompt embeds this verbatim.
# Both views must cite from this list so retrospectives are comparable.
FAILURE_MODES = """\
- **Wrong direction**: answer didn't address what was asked.
- **Wrong length**: 10-sentence answer where 1-2 would do; OR cut off too short
  on a question that needed depth.
- **Confidently incorrect**: stated a fact that's wrong.
- **Missed core concept**: didn't surface knowledge they almost certainly know.
- **Lost the thread**: started strong, drifted off, never landed.
- **Filler-heavy**: "um", "kinda", "you know" weakening signal.
- **No structure**: no "first / second / finally" signposting; reviewer had to
  reconstruct the answer.
- **Skipped clarification**: jumped to solution before scoping the problem.
- **Couldn't take a hint**: interviewer redirected, candidate didn't pivot.
- **Broke the format**: pseudocode when asked for runnable; design without
  asking constraints; behavioral answer with no specifics.
- **Defensive / argumentative**: pushed back instead of engaging the question.
- **Tone mismatch**: too casual or too stiff for the conversation.
- **Time-management failure**: ran out of time on a problem that should've fit.
- **Over-explained known concepts**: spent too long defining basics, leaving
  no time for the actual answer.
- **Buried the lead**: the strongest signal came at the end, easy to miss.
"""

# Stage-specific drill recommendations. Both views may pick from this bank.
STAGE_DRILL_BANK: dict[str, str] = {
    "intro": (
        "- Sharpen 2-min elevator pitch: who you are + 1 concrete recent win + what you want next.\n"
        "- Pre-write 'why this company' with 2 specifics tied to their product/values, not generic praise.\n"
        "- Rehearse 'walk me through your resume' → keep under 90 seconds, lead with most relevant role."
    ),
    "tech": (
        "- Drill the topic you fumbled: 5 LeetCode mediums in 3 days on it (e.g. graphs/BFS, dynamic programming, intervals).\n"
        "- Before coding: state assumptions out loud, ask 1 clarifying question, write 2 edge cases.\n"
        "- Practice naming time/space complexity unprompted.\n"
        "- For language idioms you tripped on: re-watch a 15-min cheatsheet (Python comprehensions, Go context, JS event loop)."
    ),
    "panel": (
        "- Address the questioner first, then briefly include the others ('happy to expand for the rest of the panel').\n"
        "- When stuck: think aloud about the tradeoff, don't go silent.\n"
        "- Have 2-3 cross-functional stories ready: one with PM disagreement, one with ambiguous spec, one with a tight deadline."
    ),
    "live_coding": (
        "- Mock 3 timed sessions (45 min, screen share) on platforms like Pramp or interviewing.io.\n"
        "- Build a 'pre-flight checklist': clarify input format → state approach → write tests first OR after.\n"
        "- Don't fight the interviewer — if hinted, pivot quickly.\n"
        "- Save 5 min at the end for testing & complexity recap."
    ),
    "system_design": (
        "- Pick 3 canonical drills you can reproduce in 45 min: URL shortener, rate limiter, news feed (or Twitter timeline).\n"
        "- For each: practice the 4-step ritual — clarify scale + SLA → high-level boxes → deep dive on 1 component → tradeoffs.\n"
        "- Drill the building block you fumbled (consistency models, caching layers, queue backpressure, sharding).\n"
        "- Practice the 'tradeoff sentence': '<choice A> over <choice B> because <constraint>; the cost is <tradeoff>.'"
    ),
    "cultural": (
        "- Pre-write 5 STAR stories covering: conflict, failure, leadership, ambiguity, impact.\n"
        "- For each story: quantify Result (numbers, % change). 'Improved performance' < '30% latency drop'.\n"
        "- Practice closing each story with one Reflection sentence — what you'd do differently.\n"
        "- For 'tell me about yourself': 90 sec max, ends with what you're looking for next."
    ),
    "final": (
        "- Final round questions are usually about scope, judgment, and motivation — not raw skill.\n"
        "- Have 3 sharp questions ready for the panel that signal you've done homework on their roadmap.\n"
        "- Be ready to discuss salary expectation: anchor with a range tied to market data + your seniority.\n"
        "- Close strong: name 1 reason this is your top choice, ask for next steps."
    ),
    "ai_interview": (
        "- AI interviewers reward structured answers — explicit signposting matters more than to a human.\n"
        "- Speak slightly slower than usual; transcript quality drives the eval.\n"
        "- Repeat the question briefly before answering — anchors the response and gives you a beat to think.\n"
        "- Quantify everything you can; keywords drive scoring."
    ),
}
