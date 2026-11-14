# Writing guide: cover letters, application emails, open answers

These are the rules that [`jobcopilot/writing/`](../jobcopilot/writing/) and
[`jobcopilot/answers.py`](../jobcopilot/answers.py) enforce. They come from
career-office guidance (sources at the end), adjusted for software-engineering
roles and for a generator that may only use **confirmed facts**. Every draft is
shown to the user before use; nothing is sent or submitted automatically.

Length limits, the default salutation, the minimum number of cited facts and
the number of model attempts are knobs in
[`config/writing.yaml`](../config/writing.yaml); defaults live in
`jobcopilot/writing/style.py`. This guide explains what each rule is for.

## Principles

1. **Short beats complete.** The letter sits next to the résumé and shouldn't
   repeat it. Engineering readers skim, and short-letter guides agree that a
   few tight paragraphs beat a full page. The body has a target range and a
   hard limit (`cover_letter.target_words`, `cover_letter.max_words`). An
   over-long draft is retried once, then trimmed by whole sentences, the
   least-evidenced first, and the trim is reported.
2. **Structure** (MIT CAPD, Harvard OCS, Stanford worksheet):
   - *Opening*: name the role, plus one specific thing from the posting that
     the applicant's work connects to. Say how the role was found only if it's
     a referral or an event.
   - *Evidence*: one or two paragraphs. Map two or three of the JD's must-haves
     to concrete results, citing at least one fact for each
     (`cover_letter.min_facts_cited` overall). Choose the facts that answer the
     posting rather than summarising the résumé.
   - *Close*: a sentence or two on what the applicant would like to discuss,
     then thanks. Restate interest without grovelling.
3. **Specific about them, honest about the applicant.** Company statements may
   come only from the JD or the company record; nothing is invented about
   funding, customers or news. Applicant statements may come only from cited,
   confirmed facts. A requirement the facts can't back is left out, not hedged.
4. **Numbers stay as written.** "200+", "30%": a reworded metric counts as a
   new metric.
5. **Salutation.** "Dear Hiring Manager," unless a name is known
   (`cover_letter.salutation`). Never "To whom it may concern".
6. **No filler.** The denylist (`writing/style.py: CLICHES`) covers lines like
   *I am writing to express my interest*, *passionate about*, *team player*,
   *perfect fit*, *proven track record*, *hit the ground running* and
   *world-class*. Hiring-manager surveys and career offices both single out
   the stock opener as the most damaging line. The generator deletes any
   sentence that contains one and says so.
7. **No machine tells.** Leak phrases (*As an AI*, *Here is your draft*,
   `[Company]`, `{name}`, fact ids) trigger a fresh retry, and a draft that
   still has them is marked `needs_review`.

## Application emails

- **Subject**: built in code from the role, the company and the applicant's
  name, never written by the model, and short enough to read in an inbox list.
  If the posting has a requisition ID, it's worth adding by hand.
- **Body**: a few sentences under a word limit (`email.application.max_words`).
  Give the role and the single most relevant result, add one more result tied
  to a must-have, and say the résumé is attached. Close with the signature from
  `answers.yaml: writing.signoff`, with its placeholders filled in by code.

## Follow-ups

- **When**: after a reasonable wait unless the posting gives a timeline
  (career guides suggest a week or two). The app warns when a follow-up is
  drafted sooner than `email.follow_up.min_days_after_submit` after
  `submitted_at`.
- **How often**: follow up **once**. If there's no reply, move on.
- **Content**: short (`email.follow_up.max_words`). The opening line, with the
  date of the application, is written by code from the tracker. Then one
  relevant fact and a polite question about next steps. The subject is built in
  code: `Following up: <Role> application — <Name>`.

## Open form questions ("Why us?", "Tell us about a project")

These use the same grounding as letters and stay within the form's own word or
character limit, or `answers.max_words` when the form gives none. They are
always marked **needs you**: the text is a draft for the user to edit and is
never autofilled as-is.

## Grounding (fail closed)

Each sentence is checked against the facts its paragraph cites (a cited bullet
brings in its role or project header). Sentences that aren't about the
applicant may also draw on the JD and the company record. The check flags a
sentence for any of these:

- a skill, number, title or ownership claim, or proper noun, that its evidence
  doesn't support;
- a detail moved between experiences (naming one employer narrows the evidence
  to that employer's facts; mixing two is flagged);
- the target company or title used as if it were past experience;
- a cited id that isn't a confirmed fact.

Any flag sets the status to `needs_review`. With no confirmed facts, nothing is
generated (`refused`) and the model is never called.

## Example skeletons (original, for shape only)

**Cover letter**

> Dear Hiring Manager,
>
> The Backend Engineer posting describes moving a monolith's billing logic into
> Go services. That is the kind of migration I have been doing for the past year.
>
> At ⟨employer⟩ I built ⟨service⟩ handling ⟨metric from fact⟩, which covers the
> posting's ⟨must-have 1⟩ and ⟨must-have 2⟩.
>
> I also ⟨second fact, with its number unchanged⟩, which is close to the
> ⟨must-have 3⟩ work your team lists.
>
> I'd be glad to talk through how I'd approach ⟨JD problem⟩. Thank you for
> reading.
>
> Best regards, ⟨name⟩

**Application email**

> Subject: Application: Data Engineer — ⟨Name⟩
>
> Hello ⟨Company⟩ team,
>
> I'm applying for the Data Engineer role. At ⟨employer⟩ I ⟨fact matching the
> top must-have⟩. I've also ⟨fact matching a second must-have⟩. My résumé is
> attached, and I'm happy to share more detail on either.
>
> ⟨signature⟩

**Follow-up**

> I applied for the ⟨Role⟩ role on ⟨date⟩ and wanted to follow up. My work on
> ⟨fact⟩ lines up with ⟨JD need⟩. Is there an update, or anything else I can
> send?

## Sources

- MIT CAPD, "How to write an effective cover letter (with samples)":
  https://capd.mit.edu/resources/how-to-write-an-effective-cover-letter/
  (one page, opening / body paragraphs / close, tailor each letter,
  "Dear Hiring Manager" when no name is known)
- Harvard FAS Mignone Center (OCS), "HES: Create impactful resumes and cover
  letters": https://careerservices.fas.harvard.edu/resources/hes-create-impactful-resumes-and-cover-letters/
  (the opening states the role and how you heard; the middle connects your
  story to the position)
- Stanford Career Education, "Cover Letter Worksheet":
  https://careered.stanford.edu/sites/g/files/sbiybj22801/files/media/file/cover_letter_handout.pdf
  (paragraph-by-paragraph prompts)
- Resume.io, "Short cover letter samples": https://resume.io/blog/short-cover-letter-samples
  (keep it short; avoid the "I am writing to apply" opener)
- Resume Optimizer Pro, "Software engineer cover letter examples":
  https://resumeoptimizerpro.com/blog/software-engineer-cover-letter-examples
  (hiring-manager survey figures on generic openers)
- Indeed Career Guide, "How to write a follow-up email after a job application":
  https://www.indeed.com/career-advice/finding-a-job/follow-up-email-after-application
  (wait before following up, use a clear subject line, follow up once)
