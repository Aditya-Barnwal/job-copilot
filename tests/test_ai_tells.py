"""The AI-tell detector (jobcopilot/writing/style.py find_ai_tells): every tell
is caught, and ordinary human writing is left alone. Pure functions: no LLM,
no profile, no database."""
from __future__ import annotations

import pytest

from jobcopilot.writing import style

# A plain base sentence to pad short examples to a realistic letter.
PAD = ("The order service sat behind our REST API. "
       "Pricing used to run as a nightly batch job, which was slow and failed quietly.")


def tells(text: str) -> str:
    return " | ".join(style.find_ai_tells(text))


@pytest.mark.parametrize("phrase, label", [
    ("Let me delve into the details.", "delve"),
    ("Our codebase was a rich tapestry of services.", "tapestry"),
    ("The rewrite is a testament to careful planning.", "testament to"),
    ("Search was pivotal for the product.", "pivotal"),
    ("Work in the realm of payments interests me.", "realm"),
    ("The payments landscape keeps shifting.", "landscape"),
    ("We had to navigate the complexities of billing.", "navigate the complexities"),
    ("Reviews foster a culture of learning.", "foster"),
    ("I built a robust order service.", "robust"),
    ("The cut-over went seamlessly.", "seamless"),
    ("The team uses cutting-edge tooling.", "cutting-edge"),
    ("Your innovative product caught my eye.", "innovative"),
    ("I like working in a dynamic team.", "dynamic"),
    ("I spearheaded the migration.", "spearheaded"),
    ("I honed my Go skills there.", "honed"),
    ("I am adept at debugging.", "adept at"),
    ("I am eager to contribute to the team.", "eager to contribute"),
    ("I am excited about the opportunity to work on payments.", "excited about the opportunity"),
    ("This role aligns perfectly with my background.", "aligns with"),
    ("Your mission resonates with me.", "resonates with"),
    ("I have a keen interest in databases.", "keen interest"),
    ("I bring a unique blend of skills.", "unique blend of"),
    ("I would be a valuable asset.", "valuable asset"),
    ("I want to contribute to your team's success.", "contribute to your team's success"),
    ("In today's fast-paced world, speed matters.", "in today's ... world"),
    ("In today’s digital economy, uptime matters.", "in today's ... world"),
    ("The tooling is ever-evolving.", "ever-evolving"),
    ("School gave me a strong foundation in algorithms.", "strong foundation"),
    ("I would welcome the opportunity to talk.", "I would welcome the opportunity"),
    ("Thank you for considering my application.", "thank you for considering my application"),
    ("I look forward to the possibility of joining.", "I look forward to the possibility"),
])
def test_each_ai_word_is_detected(phrase, label):
    found = style.find_ai_tells(PAD + " " + phrase)
    assert found and found[0].startswith("AI-sounding wording") and f'"{label}"' in found[0], found


@pytest.mark.parametrize("text, expect", [
    (PAD + " Furthermore, it saved money.", "stock transition (Furthermore)"),
    (PAD + " Moreover, it saved money.", "stock transition (Moreover)"),
    (PAD + " Additionally, it saved money.", "stock transition (Additionally)"),
    (PAD + " In addition, it saved money.", "stock transition (In addition)"),
    (PAD + " It was not only faster but also cheaper.", '"not only ... but also"'),
    (PAD + " I wrote Go, Python, and SQL. I cared about speed, cost, and uptime.", "lists of three"),
    (PAD + " It worked — mostly.", "em or en dash"),
    (PAD + " It ran 2019–2021.", "em or en dash"),
    (PAD + " It was slow; it was flaky; it was old.", "semicolons"),
    ("As a backend engineer, I like queues. " + PAD, 'opens with "As a ...,"'),
    (PAD + " It worked!", "exclamation mark"),
])
def test_each_structure_tell_is_detected(text, expect):
    assert expect in tells(text)


def test_mostly_i_sentences_are_detected():
    text = ("I built the order service. I moved pricing into PostgreSQL. I wrote the tests "
            "for the payment flow. I fixed the deploy script. The team liked it.")
    assert '4 of 5 sentences start with "I"' in tells(text)


def test_uniform_sentence_lengths_are_detected():
    text = ("I built a small order service for the shop. It handled every request we sent at it. "
            "The team used it daily for about two years. Pricing moved into the database after that. "
            "Runtime dropped and the late night pages stopped.")
    assert "sentence lengths are very uniform" in tells(text)


def test_same_length_paragraphs_are_detected():
    paras = ["Initrode builds payment tools for small shops across the country. The posting asks "
             "for Go and careful SQL work.",
             "At Acme I built a Go order service that sat behind our REST API. Later on, it handled "
             "all the checkout traffic.",
             "I also moved widget pricing out of a nightly batch job and into the database. "
             "That cut the runtime noticeably.",
             "How your team plans to split billing out of the monolith is what I'd like to talk "
             "about. Thanks for reading."]
    assert "every paragraph is about the same length" in tells("\n\n".join(paras))


def test_thresholds_come_from_config():
    text = PAD + " It was slow; it was flaky; it was old."
    assert "semicolons" in tells(text)
    assert not style.find_ai_tells(text, {"max_semicolons": 5})
    assert style.settings()["tells"]["max_semicolons"] >= 1          # config/writing.yaml merged


# --------------------------------------------------------------------------- no false positives

HUMAN = [
    # a natural cover letter: varied rhythm, one semicolon, one short list, contractions
    """Your posting says the payments team is pulling billing out of a Rails monolith and into Go services. I've spent most of the last year doing that kind of move, one endpoint at a time.

At Acme Widgets I wrote the Go order service that now sits behind our REST API. It handles 200+ requests per second. The hard part wasn't the throughput; it was keeping the old and new paths returning the same totals while we cut traffic over.

I also moved widget pricing out of a nightly batch job and into PostgreSQL. Runtime dropped by 30%, and the on-call pages about late price files stopped.

How your team plans to handle the cutover for existing invoices would be a good thing to talk through. Thanks for reading.""",
    # short and personal
    """I started writing software because our school's attendance sheet kept losing data. That was a long time ago, but I still like the same kind of problem: boring systems that people depend on every day.

Last spring I rebuilt the search index for a small library app. Queries that took a few seconds now come back almost instantly, and the librarians stopped keeping a paper backup.

Happy to walk through how I tested it before the switch. Thank you.""",
    # a story, technical words that look like tells but are literal
    """We shipped the mobile release two weeks late. Nobody on the team was surprised, and that bothered me more than the delay did. So I went back through every ticket from the quarter and wrote down where the time went.

Most of it was waiting. Builds took forty minutes, reviews sat for a day, and the staging database was shared by three teams. In 2023, I fixed the build cache, and it now finishes in under ten.

The app stores drafts in Realm and switches to landscape mode on tablets. The pricing code uses dynamic programming, which took me a while to get right.""",
    # an email-length note
    """I'm writing about the Data Engineer opening. At Globex I moved our event pipeline from cron jobs to Airflow, and failed runs now page someone instead of failing silently. My résumé is attached.""",
]


@pytest.mark.parametrize("text", HUMAN)
def test_human_writing_is_not_flagged(text):
    assert style.find_ai_tells(text) == []


def test_empty_text_is_clean():
    assert style.find_ai_tells("") == [] and style.find_ai_tells("Thanks.") == []
