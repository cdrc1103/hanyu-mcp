# Learning Algorithm — SM-2 Spaced Repetition

## What is SM-2?

SM-2 (SuperMemo 2, 1987) is a spaced repetition algorithm that schedules reviews at increasing intervals. Cards you know well reappear less often; cards you struggle with reappear sooner. The goal: maximise retention with minimum review time.

---

## State per word

Each vocabulary word carries three mutable fields updated after every review:

| Field | Default | Meaning |
|---|---|---|
| `interval` | 1 | Days until the next review |
| `ease_factor` | 2.5 | Difficulty multiplier (min 1.3) |
| `repetitions` | 0 | Consecutive successful reviews |
| `next_review` | today | Absolute date of next review |

---

## Quality scores

After flipping a card the user rates recall quality (0–5):

| Button | Score | Meaning |
|---|---|---|
| Again | 0 | Complete blackout |
| Hard | 2 | Wrong, but word felt familiar |
| Okay | 3 | Correct, with difficulty |
| Good | 4 | Correct after slight hesitation |
| Easy | 5 | Perfect, instant recall |

Scores 0–2 are **failures** (quality < 3). Score 3–5 are **passes**.

---

## Algorithm (`backend/database.py:54`)

```python
def apply_sm2(interval, ease, reps, quality):
    if quality < 3:                          # failure — reset streak
        return 1, ease, 0

    if reps == 0:
        new_interval = 1                     # first pass → review tomorrow
    elif reps == 1:
        new_interval = 6                     # second pass → review in 6 days
    else:
        new_interval = round(interval * ease)  # subsequent → grow by EF

    new_ease = ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ease = max(1.3, new_ease)

    return new_interval, new_ease, reps + 1
```

### Ease factor delta by quality

| Quality | EF change | Example (EF 2.5 → …) |
|---|---|---|
| 5 (Easy) | +0.10 | 2.60 |
| 4 (Good) | +0.00 | 2.50 |
| 3 (Okay) | −0.14 | 2.36 |
| < 3 (fail) | unchanged | 2.50 (streak resets) |

Minimum EF is clamped to **1.3**, preventing intervals from collapsing on very difficult words.

---

## Review cycle example

Starting state: `interval=1, ease=2.5, reps=0`

| Session | Quality | New interval | New EF | New reps | Next review |
|---|---|---|---|---|---|
| 1 | 4 (Good) | 1 day | 2.50 | 1 | +1 d |
| 2 | 4 (Good) | 6 days | 2.50 | 2 | +6 d |
| 3 | 4 (Good) | 15 days | 2.50 | 3 | +15 d |
| 4 | 5 (Easy) | 38 days | 2.60 | 4 | +38 d |
| 5 | 2 (Hard) | **1 day** | 2.60 | **0** | +1 d |

A single failure resets the streak and interval to 1, but **ease factor is preserved** across failures — accumulated difficulty history is not lost.

---

## Scheduling

After `apply_sm2` returns, `review_word` in `database.py:215` computes the next review date:

```python
next_review = (date.today() + timedelta(days=new_interval)).isoformat()
```

`GET /vocabulary/due` returns all words where `next_review <= today`, which populates the study session.

---

## Study modes

The PWA supports two orientations of the same card:

- **Normal mode** — front: word, back: definition + example, plus the pinyin (shown on the back; in reverse mode it is on the front instead)
- **Reverse mode** — front: pinyin, back: hanzi + English translation + example. Pinyin comes from the card's `pinyin` field (legacy cards: parsed from a `pinyin | english` definition). Cards with no recoverable pinyin fall back to front: definition, back: word + example

Both modes use the same SM-2 algorithm and quality scores, but each keeps its own schedule. Normal (forward) progress lives in `interval`, `ease_factor`, `repetitions` and `next_review`; reverse progress lives in the `reverse_`-prefixed columns. The study endpoints take `direction=forward|reverse` (default `forward`), so a reverse review never changes forward progress and vice versa. A card never reviewed in reverse is treated as due, so every existing card starts with a fresh reverse schedule.

---

## Quiz mode and audio

"Practice → Quiz" on the home screen runs an endless multiple-choice quiz. It is practice only: it never reads or writes the SM-2 schedule, so quiz answers do not affect flashcard progress, and "due" does not limit what it asks.

- **Cards:** every word in the selected section and review period (not just due words).
- **Questions:** hanzi → meaning, hanzi → pinyin, meaning → hanzi and sound → hanzi, picked at random per question (the Normal / Reverse toggle applies to flashcards only).
- **Looping:** the cards are asked once each in a random order; then a new round starts with a fresh shuffle, until you press "End quiz" or leave. A summary shows how many you answered and how many were right.
- **Options:** wrong options are other cards from the selected section (the whole deck when "All sections" is selected), preferring cards with the same character count. They ignore the review period, and a section needs at least 4 words to be quizzed.

Audio uses the browser's Web Speech API with a Mandarin system voice (Cantonese voices are skipped) at a fixed, slightly slow speed. It never plays something that gives away the answer: while you can see the hanzi, nothing is spoken, and only the sound → hanzi question plays by itself, because hearing the word is that question. After you answer, or when a flashcard is flipped, the word and its example sentence are spoken. The one audio setting is "Auto-play audio" (device setting in Review settings); a warning appears there only if the device has no Mandarin voice.

### Example sentences

A card's example is stored as three fields: `example` (hanzi only, the part that is spoken), `example_pinyin` and `example_translation`. The app shows them as three lines on the flashcard back, in the quiz result and in Browse. Cards created before the split kept all three lines in `example`; migration v6 split those three-line examples into the new columns. The `chinese-vocab-push` skill (`docs/skills/chinese-vocab-push/SKILL.md`) tells the assistant to write examples that show a word's context and grammar pattern.
