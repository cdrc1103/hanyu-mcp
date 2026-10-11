// Multiple-choice quiz. It mixes four question kinds at random and loops over
// the cards it is given, reshuffling every round, until it is stopped.
//   hanzi → meaning, hanzi → pinyin, meaning → hanzi, sound → hanzi
// It is practice only: answers never touch the flashcard (SM-2) schedule.
import { shortMeaning, speakableText, splitPinyin, toneSpans } from "./pinyin.js";
import { canSpeak, getSettings, speak, speakCard, stopSpeaking } from "./speech.js";

const OPTION_COUNT = 4;
const SIMILAR_POOL = 8; // distractors are drawn from the N most similar cards

// ── Tiny DOM helper (textContent only, never innerHTML) ──────────────────────
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  el.append(...children.filter((c) => c != null));
  return el;
}

// A word as hanzi spans coloured by tone; plain text when the tones can't be
// matched to the characters.
export function toneWord(word, pinyin, className = "") {
  const el = h("span", { class: className, lang: "zh-Hans" });
  const spans = toneSpans(word, pinyin);
  if (!spans) {
    el.textContent = word;
    return el;
  }
  for (const { ch, tone } of spans) {
    el.append(tone ? h("span", { class: `tone-${tone}`, text: ch }) : ch);
  }
  return el;
}

// The example sentence as up to three lines: hanzi, pinyin, English. Cards saved
// before the parts were split out keep whatever text they had in `example`
// (line breaks preserved). Returns null when the card has no example.
export function exampleBlock(card) {
  if (!card.example) return null;
  return h(
    "div",
    { class: "ex-block" },
    h("p", { class: "ex-zh", lang: "zh-Hans", text: card.example }),
    card.example_pinyin ? h("p", { class: "ex-py", text: card.example_pinyin }) : null,
    card.example_translation ? h("p", { class: "ex-en", text: card.example_translation }) : null
  );
}

function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// ── Question kinds ────────────────────────────────────────────────────────────
const meaningOf = (c) => c.heisig?.keyword || shortMeaning(splitPinyin(c).english);
const pinyinOf = (c) => splitPinyin(c).pinyin;

// label(card) is what an option shows, and what must differ between options.
const KINDS = {
  hz2en: { label: meaningOf, hanziOptions: false, autoPrompt: "audioFirst" },
  hz2py: { label: pinyinOf, hanziOptions: false, autoPrompt: false },
  en2hz: { label: (c) => c.word, hanziOptions: true, autoPrompt: false },
  snd2hz: { label: (c) => c.word, hanziOptions: true, autoPrompt: "always" },
};

function kindsFor(card) {
  const kinds = [];
  if (meaningOf(card)) kinds.push("hz2en", "en2hz");
  if (pinyinOf(card)) kinds.push("hz2py");
  if (canSpeak() && speakableText(card.word)) kinds.push("snd2hz");
  return kinds;
}

// Pick distractors that differ in label from the answer and from each other,
// preferring cards that look alike (same character count, same kind of card).
function distractors(card, kind, pool) {
  const { label } = KINDS[kind];
  const answer = label(card);
  const len = [...card.word].length;
  const similarity = (c) =>
    ([...c.word].length === len ? 2 : 0) + (!!c.heisig === !!card.heisig ? 1 : 0);
  const seen = new Set([answer]);
  const candidates = shuffle(pool)
    .filter((c) => c.id !== card.id)
    .sort((a, b) => similarity(b) - similarity(a))
    .filter((c) => {
      const text = label(c);
      if (!text || seen.has(text)) return false;
      seen.add(text);
      return true;
    });
  return shuffle(candidates.slice(0, SIMILAR_POOL)).slice(0, OPTION_COUNT - 1);
}

// A random question kind for the card; the kind asked last time is tried last so
// consecutive questions differ whenever the card allows it.
function buildQuestion(card, pool, previousKind) {
  const kinds = shuffle(kindsFor(card)).sort(
    (a, b) => (a === previousKind ? 1 : 0) - (b === previousKind ? 1 : 0)
  );
  for (const kind of kinds) {
    const others = distractors(card, kind, pool);
    if (others.length === OPTION_COUNT - 1) {
      return { card, kind, options: shuffle([card, ...others]) };
    }
  }
  return null;
}

// ── Quiz controller ───────────────────────────────────────────────────────────
// root:   element the quiz renders into
// hooks:  { onProgress(text), onFinish({asked, correct}) }
export function createQuiz(root, hooks) {
  let cards = [];
  let pool = [];
  let queue = [];
  let index = 0;
  let round = 0;
  let asked = 0;
  let correctCount = 0;
  let questionsThisRound = 0;
  let previousKind = null;
  let current = null; // { question, answered, picked }
  let active = false;

  function stop() {
    active = false;
    document.removeEventListener("keydown", onKey);
    stopSpeaking();
    root.replaceChildren();
  }

  function finish() {
    stop();
    hooks.onFinish({ asked, correct: correctCount });
  }

  // cards: the cards to loop over; fullDeck: where wrong options come from.
  function start({ cards: cardList, fullDeck }) {
    stop();
    active = true;
    cards = cardList;
    pool = fullDeck;
    queue = [];
    index = 0;
    round = 0;
    asked = 0;
    correctCount = 0;
    questionsThisRound = 0;
    previousKind = null;
    document.addEventListener("keydown", onKey);
    next();
  }

  // Begin a new round: every card once, in a fresh random order. The first card
  // is never the one that ended the previous round.
  function newRound() {
    const last = queue[queue.length - 1];
    queue = shuffle(cards);
    if (queue.length > 1 && queue[0] === last) queue.push(queue.shift());
    index = 0;
    round++;
    questionsThisRound = 0;
  }

  function next() {
    // Skip cards that can't make a question (e.g. no other card has a different
    // answer). A whole round without a single question ends the quiz instead of
    // looping forever.
    for (;;) {
      if (index >= queue.length) {
        if (round > 0 && questionsThisRound === 0) return finish();
        newRound();
      }
      const card = queue[index];
      const question = buildQuestion(card, pool, previousKind);
      if (question) {
        questionsThisRound++;
        previousKind = question.kind;
        current = { question, answered: false, picked: null };
        hooks.onProgress(`Round ${round} · ${index + 1} / ${queue.length}`);
        renderQuestion();
        return;
      }
      index++;
    }
  }

  function renderQuestion() {
    const { question } = current;
    const { card, kind } = question;
    const { pinyin } = splitPinyin(card);
    const speakOnPrompt = KINDS[kind].autoPrompt;

    const prompt = h("div", { class: "quiz-prompt" });
    if (kind === "hz2en" || kind === "hz2py") {
      prompt.append(
        h("p", {
          class: "quiz-label",
          text: kind === "hz2en" ? "What does it mean?" : "Which pinyin is it?",
        }),
        // Tone colours on the hanzi would give away the answer to the pinyin question.
        kind === "hz2py"
          ? h("span", { class: "quiz-hanzi", lang: "zh-Hans", text: card.word })
          : toneWord(card.word, pinyin, "quiz-hanzi")
      );
    } else if (kind === "snd2hz") {
      prompt.append(
        h("p", { class: "quiz-label", text: "Listen, then choose the word" }),
        h("button", {
          class: "btn btn-secondary quiz-play",
          type: "button",
          text: "🔊 Play sound",
          onclick: () => speak(card.word),
        })
      );
    } else {
      prompt.append(
        h("p", { class: "quiz-label", text: "Which word is this?" }),
        h("p", { class: "quiz-meaning", text: meaningOf(card) })
      );
    }

    const opts = h("div", { class: "quiz-options", role: "group", "aria-label": "Answers" });
    question.options.forEach((option, i) => {
      const btn = h("button", {
        class: `quiz-option${KINDS[kind].hanziOptions ? " quiz-option-hanzi" : ""}`,
        type: "button",
        "data-id": String(option.id),
        onclick: () => answer(option.id),
      });
      btn.append(
        h("span", { class: "quiz-key", "aria-hidden": "true", text: String(i + 1) }),
        // Plain text: tone colours on the options would leak the tone of the answer.
        h("span", {
          text: KINDS[kind].label(option),
          ...(KINDS[kind].hanziOptions ? { lang: "zh-Hans" } : {}),
        })
      );
      opts.append(btn);
    });

    root.replaceChildren(
      prompt,
      opts,
      h("div", { class: "quiz-result", "aria-live": "polite" }),
      h("button", {
        class: "link-btn quiz-stop",
        type: "button",
        text: "End quiz",
        onclick: finish,
      })
    );

    // Called synchronously from the tap that got us here, which keeps iOS happy.
    // Hearing a word while you can see its hanzi gives away the reading you are
    // meant to recall, so that is opt-in ("Hear the word first").
    const cfg = getSettings();
    if (
      speakOnPrompt === "always" ||
      (speakOnPrompt === "audioFirst" && cfg.autoplay && cfg.audioFirst)
    ) {
      speak(card.word);
    }
  }

  function answer(pickedId) {
    if (!current || current.answered) return;
    current.answered = true;
    current.picked = pickedId;
    const { question } = current;
    const { card } = question;
    const correct = pickedId === card.id;
    asked++;
    if (correct) correctCount++;

    for (const btn of root.querySelectorAll(".quiz-option")) {
      btn.disabled = true;
      const id = Number(btn.dataset.id);
      if (id === card.id) btn.classList.add("right");
      else if (id === pickedId) btn.classList.add("wrong");
    }

    const { pinyin, english } = splitPinyin(card);
    const result = root.querySelector(".quiz-result");
    const nextBtn = h("button", {
      class: "btn btn-primary quiz-next",
      type: "button",
      text: "Next →",
      onclick: () => {
        index++;
        next();
      },
    });
    result.replaceChildren(
      h("p", {
        class: `quiz-verdict ${correct ? "ok" : "no"}`,
        text: correct ? "✓ Correct" : "✗ Not quite",
      }),
      toneWord(card.word, pinyin, "quiz-hanzi quiz-hanzi-sm"),
      pinyin ? h("p", { class: "quiz-pinyin", text: pinyin }) : null,
      h("p", { class: "quiz-english", text: english }),
      exampleBlock(card),
      h(
        "div",
        { class: "quiz-actions" },
        h("button", {
          class: "btn btn-secondary",
          type: "button",
          text: "🔊 Hear it",
          onclick: () => speakCard(card),
        }),
        nextBtn
      )
    );
    nextBtn.focus({ preventScroll: true });
    if (getSettings().autoplay) speakCard(card);
  }

  function onKey(e) {
    if (!active || !current || e.metaKey || e.ctrlKey || e.altKey) return;
    if (/^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName)) return;
    const n = Number(e.key);
    if (!current.answered && n >= 1 && n <= OPTION_COUNT) {
      const option = current.question.options[n - 1];
      if (option) answer(option.id);
    } else if (current.answered && e.key === "Enter") {
      e.preventDefault();
      root.querySelector(".quiz-next")?.click();
    }
  }

  return { start, stop };
}
